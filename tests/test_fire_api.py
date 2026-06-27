"""Интеграционные тесты fire-роутов aiohttp (авторизация + сквозной ignite→sync)."""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.api.server import build_app
from bot.services.fire_coordinator import FireCoordinator, FireConfig


class FakeDB:
    async def get_objmapper_link_by_token(self, token):
        if token == "good":
            return {"discord_user_id": "u1", "samp_nick": "Nick"}
        return None


class FakeBot:
    def __init__(self):
        self.db = FakeDB()
        self.fire = FireCoordinator(FireConfig())


IP = "1.2.3.4:7777"
AUTH = {"Authorization": "Bearer good"}


@pytest.fixture
async def client():
    bot = FakeBot()
    app = build_app(bot)
    async with TestClient(TestServer(app)) as c:
        c.bot = bot
        yield c


async def test_sync_requires_token(client):
    r = await client.post("/api/objmapper/fire/sync", json={"server_ip": IP, "pos": {}})
    assert r.status == 401


async def test_ignite_then_sync_place(client):
    # Поджог
    r = await client.post("/api/objmapper/fire/ignite", headers=AUTH, json={
        "x": 10, "y": 20, "z": 3, "nx": 0, "ny": 0, "nz": 1, "server_ip": IP,
    })
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    gk = body["gridKey"]

    # Постановка объекта → ячейка burning, видна в cells_near
    r = await client.post("/api/objmapper/fire/sync", headers=AUTH, json={
        "server_ip": IP, "pos": {"x": 10, "y": 20, "z": 3},
        "placed": [{"gridKey": gk, "serverId": 777}],
    })
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    near = body["cells_near"]
    assert len(near) == 1 and near[0]["state"] == "burning"
    # координатор подхватил serverId
    cell = next(iter(client.bot.fire.cells.values()))
    assert cell.server_object_id == 777


async def test_ignite_bad_coords(client):
    r = await client.post("/api/objmapper/fire/ignite", headers=AUTH,
                          json={"server_ip": IP})
    assert r.status == 400


async def test_sync_requires_server_ip(client):
    r = await client.post("/api/objmapper/fire/sync", headers=AUTH, json={"pos": {}})
    assert r.status == 400
