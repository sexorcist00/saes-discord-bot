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

    auth_token = uuid.uuid4().hex
    await db.mark_objmapper_token_used(token)
    await db.upsert_objmapper_link(rec["discord_user_id"], nick, auth_token)
    logger.info(
        f"ObjMapper: привязка user={rec['discord_user_id']} nick={nick!r} "
        f"(ролей: {len(roles)})"
    )
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
    now = time.monotonic()
    if now - _version_last_write.get(auth_token, 0) >= _VERSION_THROTTLE_S:
        _version_last_write[auth_token] = now
        try:
            await db.touch_objmapper_link(auth_token, version)
        except Exception as e:  # noqa: BLE001 — не валим валидацию из-за записи метрики
            logger.warning(f"ObjMapper: touch_objmapper_link failed: {e}")

    return web.json_response({"ok": True, "nick": link["samp_nick"], "roles": roles})


def build_app(bot) -> web.Application:
    """Собрать aiohttp-приложение с роутами ObjMapper."""
    app = web.Application(middlewares=[_error_middleware])
    app["bot"] = bot
    app.add_routes(
        [
            web.get("/health", handle_health),
            web.post("/api/objmapper/link", handle_link),
            web.get("/api/objmapper/validate", handle_validate),
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
