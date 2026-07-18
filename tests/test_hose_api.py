"""Интеграционные тесты синка рукавных линий (hose_create/remove/state, TTL по дисконнекту)."""

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.api.server import build_app
from bot.services.fire_coordinator import FireCoordinator, FireConfig
from bot.services.hose_registry import (HoseRegistry, sanitize, sanitize_shape,
                                        sanitize_knock, SHAPE_MIN_INTERVAL,
                                        KNOCK_MIN_INTERVAL)


class FakeDB:
    async def get_objmapper_link_by_token(self, token):
        links = {"good": ("u1", "Nick"), "good2": ("u2", "Second")}
        if token in links:
            uid, nick = links[token]
            return {"discord_user_id": uid, "samp_nick": nick}
        return None


class FakeConfig:
    def get_main_server_id(self):
        return 1

    def get_fire_admin_role_ids(self):
        return []

    def get_fire_allowed_role_ids(self):
        return []


class FakeBot:
    def __init__(self):
        self.db = FakeDB()
        self.fire = FireCoordinator(FireConfig(place_range=50))
        self.fire_ws = {}
        self.config = FakeConfig()

    def get_guild(self, gid):
        return None


IP = "1.2.3.4:7777"

HOSE = {"t": "hose_create", "server_ip": IP, "id": "Nick:1", "owner": "Nick",
        "a": {"vehId": 123, "model": 407, "connector": "rear"},
        "b": {"playerId": 45}, "lengthM": 20, "radius": 0.08}


@pytest.fixture
async def client():
    bot = FakeBot()
    app = build_app(bot)
    async with TestClient(TestServer(app)) as c:
        c.bot = bot
        yield c


async def _hello(client, token="good"):
    ws = await client.ws_connect("/api/objmapper/fire/ws")
    await ws.send_str(json.dumps({"t": "hello", "token": token,
                                  "server_ip": IP, "pos": {"x": 0, "y": 0, "z": 0}}))
    return ws


async def _recv_until(ws, t, limit=10):
    for _ in range(limit):
        msg = await ws.receive_json()
        if msg.get("t") == t:
            return msg
    raise AssertionError(f"не дождались сообщения t={t}")


# ── Юнит: sanitize/лимиты реестра ──

def test_sanitize_accepts_and_clamps():
    clean = sanitize({"id": "n:1", "owner": "x" * 99,
                      "a": {"vehId": "123", "connector": "rear-long-name-over"},
                      "b": {"playerId": 7}, "lengthM": 9999, "radius": 0.001})
    assert clean["a"]["vehId"] == 123 and clean["b"]["playerId"] == 7
    assert clean["lengthM"] == 100.0 and clean["radius"] == 0.02
    assert len(clean["owner"]) == 32 and len(clean["a"]["connector"]) == 16


def test_sanitize_rejects_garbage():
    assert sanitize(None) is None
    assert sanitize({"id": ""}) is None
    assert sanitize({"id": "a", "a": {}, "b": {}}) is None                      # нет id-шников
    assert sanitize({"id": "a", "a": {"vehId": -5}, "b": {"playerId": 1}}) is None


def test_registry_owner_limits_and_foreign_id():
    reg = HoseRegistry()
    for i in range(4):
        assert reg.upsert("u1", IP, dict(HOSE, id=f"h{i}")) is not None
    assert reg.upsert("u1", IP, dict(HOSE, id="h5")) is None                    # лимит владельца
    assert reg.upsert("u2", IP, dict(HOSE, id="h0")) is None                    # чужой id
    assert reg.remove("h0", "u2") is None                                       # чужое не удалить
    assert reg.remove("h0", "u1") == IP
    gone = reg.remove_all_for("u1")
    assert {g["id"] for g in gone} == {"h1", "h2", "h3"}
    assert reg.snapshot(IP) == []


# ── Интеграция: WS-флоу ──

async def test_hose_create_broadcasts_to_same_server(client):
    ws1 = await _hello(client, "good")
    await _recv_until(ws1, "welcome")
    ws2 = await _hello(client, "good2")
    await _recv_until(ws2, "welcome")

    await ws1.send_str(json.dumps(HOSE))
    msg = await _recv_until(ws2, "hose_create")
    assert msg["id"] == "Nick:1" and msg["a"]["vehId"] == 123
    # отправителю эхо не идёт, но реестр пополнен
    assert "Nick:1" in client.bot.hoses.hoses

    await ws1.send_str(json.dumps({"t": "hose_remove", "id": "Nick:1", "server_ip": IP}))
    msg = await _recv_until(ws2, "hose_remove")
    assert msg["id"] == "Nick:1"
    assert client.bot.hoses.hoses == {}
    await ws1.close()
    await ws2.close()


async def test_hose_state_snapshot_for_late_joiner(client):
    ws1 = await _hello(client, "good")
    await _recv_until(ws1, "welcome")
    await ws1.send_str(json.dumps(HOSE))
    # дать серверу обработать create (следующее сообщение форсит порядок)
    await ws1.send_str(json.dumps({"t": "pos", "server_ip": IP, "pos": {"x": 0, "y": 0, "z": 0}}))

    ws2 = await _hello(client, "good2")
    snap = await _recv_until(ws2, "hose_state")
    assert len(snap["hoses"]) == 1 and snap["hoses"][0]["id"] == "Nick:1"
    await ws1.close()
    await ws2.close()


async def test_hose_ttl_on_disconnect(client):
    ws1 = await _hello(client, "good")
    await _recv_until(ws1, "welcome")
    ws2 = await _hello(client, "good2")
    await _recv_until(ws2, "welcome")

    await ws1.send_str(json.dumps(HOSE))
    await _recv_until(ws2, "hose_create")
    await ws1.close()                                     # владелец ушёл
    msg = await _recv_until(ws2, "hose_remove")
    assert msg["id"] == "Nick:1"
    assert client.bot.hoses.hoses == {}
    await ws2.close()


def test_registry_attach_updates_payload_and_owner_only():
    reg = HoseRegistry()
    assert reg.upsert("u1", IP, dict(HOSE)) is not None
    drop = {"x": 1, "y": 2, "z": 3, "dx": 1, "dy": 0, "dz": 0}
    res = reg.attach("u1", {"id": "Nick:1", "drop": drop})
    assert res["server_ip"] == IP and res["drop"]["x"] == 1.0
    assert reg.snapshot(IP)[0]["b"]["drop"]["x"] == 1.0        # снапшот видит лежащий ствол
    assert reg.attach("u2", {"id": "Nick:1", "drop": drop}) is None    # чужой не может
    assert reg.attach("u1", {"id": "Nick:1", "drop": "мусор"}) is None # мусор отвергнут
    res = reg.attach("u1", {"id": "Nick:1", "drop": False})            # поднял
    assert res["drop"] is False and "drop" not in reg.snapshot(IP)[0]["b"]


async def test_hose_attach_broadcasts(client):
    ws1 = await _hello(client, "good")
    await _recv_until(ws1, "welcome")
    ws2 = await _hello(client, "good2")
    await _recv_until(ws2, "welcome")

    await ws1.send_str(json.dumps(HOSE))
    await _recv_until(ws2, "hose_create")
    await ws1.send_str(json.dumps({"t": "hose_attach", "id": "Nick:1", "server_ip": IP,
                                   "drop": {"x": 5, "y": 6, "z": 1, "dx": 1, "dy": 0, "dz": 0}}))
    msg = await _recv_until(ws2, "hose_attach")
    assert msg["id"] == "Nick:1" and msg["drop"]["x"] == 5.0
    await ws1.send_str(json.dumps({"t": "hose_attach", "id": "Nick:1", "server_ip": IP,
                                   "drop": False}))
    msg = await _recv_until(ws2, "hose_attach")
    assert msg["drop"] is False
    await ws1.close()
    await ws2.close()


async def test_hose_rejected_reports_error(client):
    ws1 = await _hello(client, "good")
    await _recv_until(ws1, "welcome")
    await ws1.send_str(json.dumps({"t": "hose_create", "server_ip": IP, "id": ""}))
    msg = await _recv_until(ws1, "error")
    assert msg["reason"] == "HOSE_REJECTED"
    await ws1.close()


# ── Синк физики/воды: class/w/stow в create+снапшоте, water/stow/shape/knock ──

def test_sanitize_class_water_stow_fields():
    clean = sanitize(dict(HOSE, **{"class": "supply",
                                   "w": {"on": 1, "p": 7.5, "agent": "napalm"},
                                   "stow": {"mode": "ground", "x": 1, "y": 2, "z": 3,
                                            "ang": 0.5, "noz": True}}))
    assert clean["class"] == "supply"
    assert clean["w"] == {"on": True, "p": 1.0, "agent": "water"}   # клампы p и агента
    assert clean["stow"]["mode"] == "ground" and clean["stow"]["noz"] is True
    clean = sanitize(dict(HOSE, **{"class": "мусор"}))
    assert clean["class"] == "attack"                               # неизвестный класс → attack
    assert "stow" not in clean and clean.get("w") is None


def test_registry_set_water_and_stow_owner_only():
    reg = HoseRegistry()
    assert reg.upsert("u1", IP, dict(HOSE)) is not None
    res = reg.set_water("u1", {"id": "Nick:1", "on": True, "p": 0.8, "agent": "foam"})
    assert res["w"] == {"on": True, "p": 0.8, "agent": "foam"} and res["server_ip"] == IP
    assert reg.snapshot(IP)[0]["w"]["p"] == 0.8                     # снапшот видит кран
    assert reg.set_water("u2", {"id": "Nick:1", "on": True}) is None    # чужой не может
    stow = {"mode": "hand", "x": 1, "y": 2, "z": 3, "ang": 0.1}
    res = reg.set_stow("u1", {"id": "Nick:1", "stow": stow})
    assert res["stow"]["mode"] == "hand"
    assert reg.snapshot(IP)[0]["stow"]["mode"] == "hand"            # снапшот видит скатку
    assert reg.set_stow("u2", {"id": "Nick:1", "stow": stow}) is None
    assert reg.set_stow("u1", {"id": "Nick:1", "stow": {"mode": "орбита"}}) is None
    res = reg.set_stow("u1", {"id": "Nick:1", "stow": False})       # размотал
    assert res["stow"] is False and "stow" not in reg.snapshot(IP)[0]


def test_shape_sanitize_and_rate_limit():
    reg = HoseRegistry()
    assert reg.upsert("u1", IP, dict(HOSE)) is not None
    frame = {"id": "Nick:1", "k": 1, "ax": 10.0, "ay": 20.0, "az": 3.0,
             "n": [0, 0, 0, 120, -50, 10], "f": 0.5,
             "m": {"x": 1, "y": 2, "z": 3, "dx": 1, "dy": 0, "dz": 0}, "fire": True}
    res = reg.shape_ok("u1", dict(frame), now=100.0)
    assert res["n"] == [0, 0, 0, 120, -50, 10] and res["fire"] is True
    assert res["server_ip"] == IP
    # rate-limit: второй кадр раньше min-интервала — дроп; после интервала — ок
    assert reg.shape_ok("u1", dict(frame, k=2), now=100.0 + SHAPE_MIN_INTERVAL / 2) is None
    assert reg.shape_ok("u1", dict(frame, k=3), now=100.0 + SHAPE_MIN_INTERVAL + 0.01) is not None
    # чужая линия / мусор
    assert reg.shape_ok("u2", dict(frame, k=4), now=200.0) is None
    assert sanitize_shape(dict(frame, n=[1, 2])) is None            # не кратно 3
    assert sanitize_shape(dict(frame, n=list(range(3 * 200)))) is None  # >128 узлов
    assert sanitize_shape({"id": "x", "n": "мусор"}) is None


def test_knock_sanitize_and_rate_limit():
    reg = HoseRegistry()
    knock = {"pid": 45, "vx": 3.0, "vy": 0.0, "vz": 99.0}
    res = reg.knock_ok("u1", dict(knock), now=10.0)
    assert res["pid"] == 45 and res["vz"] == 50.0                   # кламп импульса
    assert reg.knock_ok("u1", dict(knock), now=10.0 + KNOCK_MIN_INTERVAL / 2) is None
    assert reg.knock_ok("u1", dict(knock), now=10.0 + KNOCK_MIN_INTERVAL + 0.01) is not None
    assert sanitize_knock({"pid": 4000}) is None                    # pid вне диапазона
    assert sanitize_knock({"pid": "мусор"}) is None


async def test_hose_water_and_stow_broadcast(client):
    ws1 = await _hello(client, "good")
    await _recv_until(ws1, "welcome")
    ws2 = await _hello(client, "good2")
    await _recv_until(ws2, "welcome")

    await ws1.send_str(json.dumps(dict(HOSE, **{"class": "supply"})))
    msg = await _recv_until(ws2, "hose_create")
    assert msg["class"] == "supply"                                 # класс доехал
    await ws1.send_str(json.dumps({"t": "hose_water", "id": "Nick:1", "server_ip": IP,
                                   "on": True, "p": 0.9, "agent": "water"}))
    msg = await _recv_until(ws2, "hose_water")
    assert msg["id"] == "Nick:1" and msg["on"] is True and msg["p"] == 0.9
    await ws1.send_str(json.dumps({"t": "hose_stow", "id": "Nick:1", "server_ip": IP,
                                   "stow": {"mode": "ground", "x": 1, "y": 2, "z": 3,
                                            "ang": 0.7}}))
    msg = await _recv_until(ws2, "hose_stow")
    assert msg["stow"]["mode"] == "ground"
    await ws1.close()
    await ws2.close()


async def test_hose_shape_relayed_not_stored(client):
    ws1 = await _hello(client, "good")
    await _recv_until(ws1, "welcome")
    ws2 = await _hello(client, "good2")
    await _recv_until(ws2, "welcome")

    await ws1.send_str(json.dumps(HOSE))
    await _recv_until(ws2, "hose_create")
    await ws1.send_str(json.dumps({"t": "hose_shape", "id": "Nick:1", "server_ip": IP,
                                   "k": 7, "ax": 1.0, "ay": 2.0, "az": 3.0,
                                   "n": [0, 0, 0, 100, 0, -20], "f": 0.75, "fire": True,
                                   "m": {"x": 4, "y": 5, "z": 1, "dx": 0, "dy": 1, "dz": 0}}))
    msg = await _recv_until(ws2, "hose_shape")
    assert msg["k"] == 7 and msg["n"] == [0, 0, 0, 100, 0, -20] and msg["f"] == 0.75
    assert msg["fire"] is True and msg["m"]["dy"] == 1.0
    assert "server_ip" not in msg                                   # служебное поле не утекает
    # реестр форму НЕ хранит: payload линии без узлов
    assert "n" not in client.bot.hoses.hoses["Nick:1"]["payload"]
    await ws1.close()
    await ws2.close()


async def test_hose_knock_relayed(client):
    ws1 = await _hello(client, "good")
    await _recv_until(ws1, "welcome")
    ws2 = await _hello(client, "good2")
    await _recv_until(ws2, "welcome")

    await ws1.send_str(json.dumps({"t": "hose_knock", "server_ip": IP,
                                   "pid": 45, "vx": 2.5, "vy": -1.0, "vz": 4.0}))
    msg = await _recv_until(ws2, "hose_knock")
    assert msg["pid"] == 45 and msg["vx"] == 2.5 and msg["vz"] == 4.0
    await ws1.close()
    await ws2.close()
