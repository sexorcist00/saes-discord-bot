"""
HTTP API авторизации ObjMapper.

Встраивается в ту же asyncio-петлю, что и discord.py, поэтому имеет прямой доступ
к объекту бота (guild.fetch_member, роли участников, БД, конфиг) без отдельного
сервиса и без IPC.

Поток авторизации:
  1. Пользователь в Discord: /objmapper link  →  6-значный одноразовый токен (TTL).
  2. Скрипт: POST /api/objmapper/link {token, nick}  →  постоянный auth_token (UUID).
  3. Скрипт при каждом старте: GET /api/objmapper/validate (Bearer)  →  живая
     перепроверка членства в сервере и наличия хотя бы одной разрешённой роли.
"""

import json
import time
import uuid
from typing import List, Optional, Tuple

import discord
from aiohttp import web

from bot.utils.logger import get_logger

logger = get_logger("api.server")

# Троттлинг записи версии/last_seen — не чаще раза в 60с на токен (как в callout).
_VERSION_THROTTLE_S = 60
_version_last_write = {}  # auth_token -> monotonic timestamp


async def check_member_roles(
    bot, discord_user_id
) -> Tuple[bool, Optional[discord.Member], List[dict], str]:
    """
    Живая проверка: пользователь в главном сервере и имеет хотя бы одну роль из
    objmapper.allowed_role_ids.

    Returns:
        (ok, member, matched_roles, reason)
        reason: 'OK' | 'GUILD_UNAVAILABLE' | 'NOT_MEMBER' | 'DISCORD_ERROR'
                | 'NO_ROLES_CONFIGURED' | 'NO_ROLE'
    """
    config = bot.config
    main_server_id = config.get_main_server_id()
    allowed = set(config.get_objmapper_allowed_role_ids())

    guild = bot.get_guild(main_server_id)
    if guild is None:
        logger.warning("ObjMapper: главный сервер недоступен (бот не в гильдии?)")
        return False, None, [], "GUILD_UNAVAILABLE"

    try:
        member = await guild.fetch_member(int(discord_user_id))
    except discord.NotFound:
        return False, None, [], "NOT_MEMBER"
    except discord.HTTPException as e:
        logger.warning(f"ObjMapper: ошибка fetch_member: {e}")
        return False, None, [], "DISCORD_ERROR"

    if not allowed:
        # Список ролей не настроен — никого не пускаем (безопасный дефолт).
        return False, member, [], "NO_ROLES_CONFIGURED"

    matched = [r for r in member.roles if r.id in allowed]
    if not matched:
        return False, member, [], "NO_ROLE"

    roles_payload = [{"id": str(r.id), "name": r.name} for r in matched]
    return True, member, roles_payload, "OK"


@web.middleware
async def _error_middleware(request, handler):
    """Любое необработанное исключение → JSON 500 (а не HTML-страница aiohttp)."""
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"ObjMapper API: необработанная ошибка: {e}", exc_info=True)
        return web.json_response({"error": "INTERNAL"}, status=500)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def handle_link(request: web.Request) -> web.Response:
    """POST /api/objmapper/link — обмен 6-значного токена на постоянный auth_token."""
    bot = request.app["bot"]
    db = bot.db
    config = bot.config

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "BAD_JSON"}, status=400)

    token = str(payload.get("token", "")).strip()
    nick = str(payload.get("nick", "")).strip()
    nmin, nmax = config.get_objmapper_nick_limits()

    if not (len(token) == 6 and token.isdigit()):
        return web.json_response({"error": "BAD_TOKEN_FORMAT"}, status=400)
    if not (nmin <= len(nick) <= nmax):
        return web.json_response({"error": "BAD_NICK"}, status=400)

    rec = await db.get_objmapper_token(token)
    if not rec or rec["is_used"]:
        return web.json_response({"error": "INVALID_TOKEN"}, status=401)
    if int(rec["expires_at"]) < time.time():
        return web.json_response({"error": "EXPIRED_TOKEN"}, status=401)

    ok, _member, roles, reason = await check_member_roles(bot, rec["discord_user_id"])
    if not ok:
        # Токен валиден, но доступа нет (не в сервере / нет роли).
        return web.json_response({"error": "NO_ACCESS", "reason": reason}, status=403)

    # Новый ли это пользователь (нет прежней привязки этого Discord-аккаунта)?
    prior = await db.get_objmapper_link_by_user(rec["discord_user_id"])

    auth_token = uuid.uuid4().hex
    await db.mark_objmapper_token_used(token)
    await db.upsert_objmapper_link(rec["discord_user_id"], nick, auth_token)
    logger.info(
        f"ObjMapper: привязка user={rec['discord_user_id']} nick={nick!r} "
        f"(ролей: {len(roles)}, новый={not prior})"
    )

    # Audit: новый пользователь скрипта (только первая привязка аккаунта).
    if prior is None and getattr(bot, "audit", None):
        try:
            await bot.audit.new_user(rec["discord_user_id"], nick, roles)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"audit new_user failed: {e}")

    return web.json_response(
        {"auth_token": auth_token, "nick": nick, "roles": roles}, status=201
    )


async def handle_validate(request: web.Request) -> web.Response:
    """GET /api/objmapper/validate — живая перепроверка по Bearer-токену."""
    bot = request.app["bot"]
    db = bot.db

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return web.json_response({"error": "NO_TOKEN"}, status=401)
    auth_token = header[7:].strip()

    link = await db.get_objmapper_link_by_token(auth_token)
    if not link:
        return web.json_response({"error": "UNAUTHORIZED"}, status=401)

    ok, _member, roles, reason = await check_member_roles(bot, link["discord_user_id"])
    if not ok:
        return web.json_response({"error": "NO_ACCESS", "reason": reason}, status=403)

    # Обновляем last_seen/версию не чаще раза в 60с на токен.
    version = request.headers.get("X-Script-Version")
    old_version = link.get("script_version")
    # Смена версии (был известен старый, пришёл другой) = пользователь обновил скрипт.
    version_changed = bool(version and old_version and version != old_version)
    now = time.monotonic()
    if version_changed or now - _version_last_write.get(auth_token, 0) >= _VERSION_THROTTLE_S:
        _version_last_write[auth_token] = now
        try:
            await db.touch_objmapper_link(auth_token, version)
        except Exception as e:  # noqa: BLE001 — не валим валидацию из-за записи метрики
            logger.warning(f"ObjMapper: touch_objmapper_link failed: {e}")

    # Audit: обновление скрипта.
    if version_changed and getattr(bot, "audit", None):
        try:
            await bot.audit.script_updated(
                link["discord_user_id"], link["samp_nick"], old_version, version
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"audit script_updated failed: {e}")

    return web.json_response({"ok": True, "nick": link["samp_nick"], "roles": roles})


async def handle_avatar(request: web.Request) -> web.Response:
    """
    GET /api/objmapper/avatar — отдаёт аватар Discord-пользователя, привязанного к токену.

    Байты скачиваются на стороне бота (discord.py Asset.read) и возвращаются как image/png —
    Lua-клиент не умеет HTTPS к cdn.discordapp.com, поэтому проксируем через бота.
    """
    bot = request.app["bot"]
    db = bot.db

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return web.json_response({"error": "NO_TOKEN"}, status=401)
    auth_token = header[7:].strip()

    link = await db.get_objmapper_link_by_token(auth_token)
    if not link:
        return web.json_response({"error": "UNAUTHORIZED"}, status=401)

    guild = bot.get_guild(bot.config.get_main_server_id())
    if guild is None:
        return web.json_response({"error": "GUILD_UNAVAILABLE"}, status=503)
    try:
        member = await guild.fetch_member(int(link["discord_user_id"]))
    except discord.NotFound:
        return web.json_response({"error": "NOT_MEMBER"}, status=403)
    except discord.HTTPException as e:
        logger.warning(f"ObjMapper avatar: fetch_member failed: {e}")
        return web.json_response({"error": "DISCORD_ERROR"}, status=502)

    try:
        asset = member.display_avatar.replace(size=64, static_format="png")
        data = await asset.read()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ObjMapper avatar: read failed: {e}")
        return web.json_response({"error": "AVATAR_FETCH_FAILED"}, status=502)

    return web.Response(
        body=data,
        content_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# Лимит размера тела телеметрии (батч-хартбит — маленький JSON).
_TELEMETRY_MAX_BODY = 16 * 1024


async def handle_telemetry(request: web.Request) -> web.Response:
    """
    POST /api/objmapper/telemetry — приём батч-хартбита статистики (Bearer).

    Роли НЕ перепроверяются на каждом хартбите (это вызов Discord API раз в минуту
    на пользователя): достаточно существования токена. Живая ревалидация членства/
    ролей и так идёт на /validate при каждом старте скрипта.
    """
    bot = request.app["bot"]
    db = bot.db

    if not bot.config.is_objmapper_telemetry_enabled():
        return web.json_response({"ok": False, "error": "DISABLED"}, status=200)

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return web.json_response({"error": "NO_TOKEN"}, status=401)
    auth_token = header[7:].strip()

    link = await db.get_objmapper_link_by_token(auth_token)
    if not link:
        return web.json_response({"error": "UNAUTHORIZED"}, status=401)

    if request.content_length and request.content_length > _TELEMETRY_MAX_BODY:
        return web.json_response({"error": "PAYLOAD_TOO_LARGE"}, status=413)

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "BAD_JSON"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "BAD_PAYLOAD"}, status=400)

    try:
        await db.apply_objmapper_telemetry(
            link["discord_user_id"], link["samp_nick"], payload
        )
    except Exception as e:  # noqa: BLE001 — не валим клиента из-за ошибки записи метрики
        logger.warning(f"ObjMapper telemetry: apply failed: {e}", exc_info=True)
        return web.json_response({"ok": False, "error": "STORE_FAILED"}, status=200)

    return web.json_response({"ok": True})


# ════════════════════════════════════════════════════════════════════════
#  Fire API — система распространения и тушения огня (координатор)
# ════════════════════════════════════════════════════════════════════════
#  Бэкенд НЕ симулирует огонь (нет геометрии GTA): он координирует. Клиенты
#  вычисляют распространение и шлют claim-заявки; координатор дедупит, ведёт
#  жизненный цикл, маршрутизирует удаление. См. services/fire_coordinator.py.

_FIRE_MAX_BODY = 64 * 1024  # sync может нести десятки ячеек


async def _bearer_link(request: web.Request):
    """Достать link по Bearer-токену. Возвращает (link, None) или (None, web.Response)."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None, web.json_response({"error": "NO_TOKEN"}, status=401)
    auth_token = header[7:].strip()
    link = await request.app["bot"].db.get_objmapper_link_by_token(auth_token)
    if not link:
        return None, web.json_response({"error": "UNAUTHORIZED"}, status=401)
    return link, None


async def _read_json_dict(request: web.Request):
    """Прочитать тело как dict с проверкой лимита. (data, None) | (None, web.Response)."""
    if request.content_length and request.content_length > _FIRE_MAX_BODY:
        return None, web.json_response({"error": "PAYLOAD_TOO_LARGE"}, status=413)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return None, web.json_response({"error": "BAD_JSON"}, status=400)
    if not isinstance(payload, dict):
        return None, web.json_response({"error": "BAD_PAYLOAD"}, status=400)
    return payload, None


def _fire_or_503(request: web.Request):
    """Координатор или 503, если fire выключен."""
    fire = request.app.get("fire")
    if fire is None:
        return None, web.json_response({"error": "FIRE_DISABLED"}, status=503)
    return fire, None


# ── WebSocket: воркер-пул + раздача заданий ─────────────────────────────────
#  Постоянное соединение каждого клиента-воркера. Бэкенд пушит задания (place/remove)
#  наименее загруженному in-range воркеру (балансировка в координаторе). Сообщения —
#  JSON-текст. Реестр сокетов живёт на bot.fire_ws {user_id -> ws}.

async def _ws_send(ws, obj) -> bool:
    if ws is None or ws.closed:
        return False
    try:
        await ws.send_str(json.dumps(obj))
        return True
    except Exception:  # noqa: BLE001
        return False


async def fire_push_dispatch(bot) -> None:
    """Раздать pending-задания и разослать их назначенным воркерам по сокетам."""
    if not getattr(bot, "fire", None):
        return
    for uid, payload in bot.fire.dispatch():
        await _ws_send(bot.fire_ws.get(uid), {"t": "job", "job": payload})


async def fire_push_states(bot) -> None:
    """Разослать каждому воркеру состояние ближних очагов (для оверлея/спреда)."""
    if not getattr(bot, "fire", None):
        return
    for uid in list(bot.fire_ws.keys()):
        await _ws_send(bot.fire_ws.get(uid), {"t": "state", **bot.fire.state_for(uid)})


async def handle_fire_ws(request: web.Request) -> web.StreamResponse:
    """GET /api/objmapper/fire/ws — постоянный канал воркера (Bearer в hello-сообщении)."""
    bot = request.app["bot"]
    fire = request.app.get("fire")
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=_FIRE_MAX_BODY)
    await ws.prepare(request)
    if fire is None:
        await _ws_send(ws, {"t": "error", "reason": "FIRE_DISABLED"})
        await ws.close()
        return ws

    user_id = None
    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                if msg.type == web.WSMsgType.ERROR:
                    break
                continue
            try:
                data = json.loads(msg.data)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(data, dict):
                continue
            t = data.get("t")

            # Первое сообщение — hello с токеном (авторизация воркера).
            if user_id is None:
                if t != "hello":
                    continue
                link = await bot.db.get_objmapper_link_by_token(str(data.get("token") or ""))
                if not link:
                    await _ws_send(ws, {"t": "error", "reason": "UNAUTHORIZED"})
                    break
                user_id = str(link["discord_user_id"])
                server_ip = str(data.get("server_ip") or "")
                fire.worker_connect(user_id, link.get("samp_nick", ""), server_ip, data.get("pos") or {})
                bot.fire_ws[user_id] = ws
                await _ws_send(ws, {"t": "welcome", "caps": fire.caps()})
                await fire_push_dispatch(bot)
                await _ws_send(ws, {"t": "state", **fire.state_for(user_id)})
                continue

            sip = str(data.get("server_ip") or "")
            if t == "pos":
                fire.worker_pos(user_id, sip, data.get("pos") or {})
            elif t == "ignite":
                try:
                    fire.ignite(user_id, float(data["x"]), float(data["y"]), float(data["z"]),
                                float(data.get("nx", 0)), float(data.get("ny", 0)),
                                float(data.get("nz", 1)), sip)
                except (KeyError, TypeError, ValueError):
                    pass
            elif t == "propose":
                fire.propose(user_id, sip, data.get("cells") or [])
            elif t == "done":
                fire.job_done(user_id, str(data.get("id")), bool(data.get("ok")), data.get("serverId"))
            elif t == "water":
                fire.apply_water(sip, data.get("cells") or [])
            # после любой мутации — раздать задания (включая только что появившиеся)
            await fire_push_dispatch(bot)
    except Exception as e:  # noqa: BLE001
        logger.warning("fire ws error user=%s: %s", user_id, e)
    finally:
        if user_id:
            fire.worker_disconnect(user_id)
            bot.fire_ws.pop(user_id, None)
            await fire_push_dispatch(bot)   # реквью раздать оставшимся
            logger.info("fire ws closed user=%s", user_id)
    return ws


async def handle_fire_admin(request: web.Request) -> web.Response:
    """POST /api/objmapper/fire/admin — старт/стоп/статус (под ролью)."""
    fire, err = _fire_or_503(request)
    if err:
        return err
    link, err = await _bearer_link(request)
    if err:
        return err
    # Админ-действия требуют живой роли (в отличие от sync/ignite — там достаточно токена).
    ok, _m, _roles, reason = await check_member_roles(request.app["bot"], link["discord_user_id"])
    if not ok:
        return web.json_response({"error": "NO_ACCESS", "reason": reason}, status=403)
    data, err = await _read_json_dict(request)
    if err:
        return err

    action = str(data.get("action") or "").strip()
    server_ip = str(data.get("server_ip") or "").strip() or None
    if action == "status":
        snap = fire.snapshot(server_ip)
        logger.info("fire admin status by user=%s: %s", link["discord_user_id"], snap)
        return web.json_response({"ok": True, "snapshot": snap})
    if action == "wipe":
        n = fire.wipe(server_ip)
        logger.info("fire admin wipe by user=%s: снято %d ячеек (server_ip=%s)",
                    link["discord_user_id"], n, server_ip)
        return web.json_response({"ok": True, "wiped": n})
    return web.json_response({"error": "BAD_ACTION"}, status=400)


def build_app(bot) -> web.Application:
    """Собрать aiohttp-приложение с роутами ObjMapper."""
    app = web.Application(middlewares=[_error_middleware])
    app["bot"] = bot
    # Координатор пожара (если включён) — общий с фоновой tick-задачей в main.py.
    app["fire"] = getattr(bot, "fire", None)
    app.add_routes(
        [
            web.get("/health", handle_health),
            web.post("/api/objmapper/link", handle_link),
            web.get("/api/objmapper/validate", handle_validate),
            web.get("/api/objmapper/avatar", handle_avatar),
            web.post("/api/objmapper/telemetry", handle_telemetry),
            web.get("/api/objmapper/fire/ws", handle_fire_ws),
            web.post("/api/objmapper/fire/admin", handle_fire_admin),
        ]
    )
    return app


async def start_api(bot) -> web.AppRunner:
    """Запустить HTTP API в текущей петле. Возвращает runner для остановки."""
    app = build_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    host = bot.config.get_objmapper_api_host()
    port = bot.config.get_objmapper_api_port()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"ObjMapper API запущен на http://{host}:{port}")
    return runner
