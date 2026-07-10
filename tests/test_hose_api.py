"""Интеграционные тесты синка рукавных линий (hose_create/remove/state, TTL по дисконнекту)."""

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.api.server import build_app
from bot.services.fire_coordinator import FireCoordinator, FireConfig
from bot.services.hose_registry import HoseRegistry, sanitize


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
