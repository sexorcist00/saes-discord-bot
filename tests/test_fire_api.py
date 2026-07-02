"""Интеграционные тесты fire WebSocket-канала (hello/job/done) + авторизация."""

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.api.server import build_app
from bot.services.fire_coordinator import FireCoordinator, FireConfig


class FakeDB:
    async def get_objmapper_link_by_token(self, token):
        if token == "good":
            return {"discord_user_id": "u1", "samp_nick": "Nick"}
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
        return None   # check_fire_roles → (False, False) без обращения к Discord


IP = "1.2.3.4:7777"


@pytest.fixture
async def client():
    bot = FakeBot()
    app = build_app(bot)
    async with TestClient(TestServer(app)) as c:
        c.bot = bot
        yield c


async def _recv_until(ws, t, limit=10):
    """Получать сообщения, пока не придёт тип t (пропуская welcome/state)."""
    for _ in range(limit):
        msg = await ws.receive_json()
        if msg.get("t") == t:
            return msg
    raise AssertionError(f"не дождались сообщения t={t}")


async def test_ws_rejects_bad_token(client):
    async with client.ws_connect("/api/objmapper/fire/ws") as ws:
        await ws.send_str(json.dumps({"t": "hello", "token": "bad", "server_ip": IP, "pos": {}}))
        msg = await ws.receive_json()
        assert msg["t"] == "error" and msg["reason"] == "UNAUTHORIZED"


async def test_ws_hello_welcome_and_worker_registered(client):
    async with client.ws_connect("/api/objmapper/fire/ws") as ws:
        await ws.send_str(json.dumps({"t": "hello", "token": "good",
                                      "server_ip": IP, "pos": {"x": 0, "y": 0, "z": 0}}))
        welcome = await _recv_until(ws, "welcome")
        assert "caps" in welcome and welcome["caps"]["grid"] == 1.2
        assert "u1" in client.bot.fire.workers


async def test_ws_ignite_dispatches_place_job(client):
    async with client.ws_connect("/api/objmapper/fire/ws") as ws:
        await ws.send_str(json.dumps({"t": "hello", "token": "good",
                                      "server_ip": IP, "pos": {"x": 0, "y": 0, "z": 0}}))
        await _recv_until(ws, "welcome")
        # Выдаём роль поджога этому воркеру (в реале — check_fire_roles на hello).
        client.bot.fire.workers["u1"].can_ignite = True
        # Поджог → бэкенд создаёт place-задание и пушит его нам (единственный воркер).
        await ws.send_str(json.dumps({"t": "ignite", "server_ip": IP,
                                      "x": 1, "y": 1, "z": 0, "nx": 0, "ny": 0, "nz": 1}))
        job = await _recv_until(ws, "job")
        assert job["job"]["kind"] == "place"
        assert "id" in job["job"] and "gridKey" in job["job"]
        # задание назначено нам (в inflight)
        assert job["job"]["id"] in client.bot.fire.workers["u1"].inflight
        # done→burning покрыт юнит-тестом координатора (test_place_done_marks_burning)
