"""Юнит-тесты FireCoordinator (модель воркер-пул + очередь заданий)."""

import pytest

from bot.services.fire_coordinator import (
    FireCoordinator, FireConfig,
    STATE_BURNING, STATE_EXTINGUISHING, STATE_OUT, KIND_PLACE, KIND_REMOVE,
)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def advance(self, dt):
        self.t += dt

    def __call__(self):
        return self.t


IP = "1.2.3.4:7777"


def make(**cfg):
    clk = FakeClock()
    return FireCoordinator(FireConfig(**cfg), clock=clk, monotonic=clk), clk


def connect(coord, uid, x=0, y=0, z=0, ip=IP):
    coord.worker_connect(uid, uid, ip, {"x": x, "y": y, "z": z})


def test_ignite_creates_place_job_and_dispatches():
    coord, _ = make()
    connect(coord, "u1", 0, 0, 0)
    res = coord.ignite("u1", 1, 1, 0, 0, 0, 1, IP)
    assert res["ok"]
    # одна ячейка proposed + одно place-задание pending
    assert len(coord.cells) == 1 and len(coord.jobs) == 1
    out = coord.dispatch()
    assert len(out) == 1
    uid, payload = out[0]
    assert uid == "u1" and payload["kind"] == KIND_PLACE


def test_place_done_marks_burning():
    coord, _ = make()
    connect(coord, "u1", 0, 0, 0)
    coord.ignite("u1", 1, 1, 0, 0, 0, 1, IP)
    out = coord.dispatch()
    job = out[0][1]
    coord.job_done("u1", job["id"], True, 555)
    cell = next(iter(coord.cells.values()))
    assert cell.state == STATE_BURNING
    assert cell.server_object_id == 555 and cell.placed_by == "u1"
    assert len(coord.jobs) == 0


def test_propose_balances_across_workers():
    coord, _ = make(max_inflight=2, place_range=50)
    connect(coord, "A", 0, 0, 0)
    connect(coord, "B", 0, 0, 0)
    cands = [{"x": 4, "y": 0, "z": 0}, {"x": 6, "y": 0, "z": 0},
             {"x": 8, "y": 0, "z": 0}, {"x": 10, "y": 0, "z": 0}]
    assert coord.propose("A", IP, cands) == 4
    coord.dispatch()
    # 4 задания, max_inflight=2 → ровно по 2 на воркера (балансировка)
    assert len(coord.workers["A"].inflight) == 2
    assert len(coord.workers["B"].inflight) == 2


def test_dispatch_respects_place_range():
    coord, _ = make(place_range=28)
    connect(coord, "far", 1000, 1000, 0)   # далеко от очага
    coord.ignite("far", 1, 1, 0, 0, 0, 1, IP)
    out = coord.dispatch()
    assert out == []                       # некому ставить (вне радиуса)
    assert len(coord.jobs) == 1            # задание ждёт


def test_water_remove_routed_to_placer_own():
    coord, _ = make(place_range=50)
    connect(coord, "A", 0, 0, 0)
    coord.ignite("A", 1, 1, 0, 0, 0, 1, IP)
    job = coord.dispatch()[0][1]
    coord.job_done("A", job["id"], True, 7)
    cell = next(iter(coord.cells.values()))
    cell.heat = 50
    coord.apply_water(IP, [{"cellId": cell.id, "amount": 60}])
    assert cell.state == STATE_EXTINGUISHING
    out = coord.dispatch()
    assert len(out) == 1
    uid, payload = out[0]
    assert uid == "A" and payload["kind"] == KIND_REMOVE
    assert payload["serverId"] == 7 and payload["mode"] == "own"


def test_remove_foreign_when_placer_offline():
    coord, clk = make(place_range=50, worker_stale_s=15)
    connect(coord, "A", 0, 0, 0)
    coord.ignite("A", 1, 1, 0, 0, 0, 1, IP)
    job = coord.dispatch()[0][1]
    coord.job_done("A", job["id"], True, 9)
    cell = next(iter(coord.cells.values()))
    cell.heat = 10
    coord.apply_water(IP, [{"cellId": cell.id, "amount": 20}])
    # placer A уходит офлайн, появляется B рядом
    coord.worker_disconnect("A")
    connect(coord, "B", 2, 2, 0)
    out = coord.dispatch()
    assert len(out) == 1
    uid, payload = out[0]
    assert uid == "B" and payload["mode"] == "foreign" and payload["serverId"] == 9


def test_worker_disconnect_requeues_jobs():
    coord, _ = make(place_range=50)
    connect(coord, "A", 0, 0, 0)
    coord.ignite("A", 1, 1, 0, 0, 0, 1, IP)
    coord.dispatch()
    assert len(coord.workers["A"].inflight) == 1
    coord.worker_disconnect("A")
    # задание снова pending
    job = next(iter(coord.jobs.values()))
    assert job.assigned_to is None


def test_job_timeout_requeues_in_tick():
    coord, clk = make(place_range=50, job_timeout_s=10, worker_stale_s=100)
    connect(coord, "A", 0, 0, 0)
    coord.ignite("A", 1, 1, 0, 0, 0, 1, IP)
    coord.dispatch()
    job = next(iter(coord.jobs.values()))
    assert job.assigned_to == "A"
    clk.advance(11)
    coord.tick()
    assert job.assigned_to is None
    assert len(coord.workers["A"].inflight) == 0


def test_wipe_clears_cells_and_jobs():
    coord, _ = make(place_range=50)
    connect(coord, "A", 0, 0, 0)
    coord.ignite("A", 1, 1, 0, 0, 0, 1, IP)
    coord.dispatch()
    assert len(coord.cells) == 1 and len(coord.jobs) == 1
    n = coord.wipe(IP)
    assert n == 1
    assert len(coord.cells) == 0 and len(coord.jobs) == 0
    assert len(coord.workers["A"].inflight) == 0


def test_propose_cap_per_incident():
    coord, _ = make(max_cells=2, place_range=50)
    connect(coord, "A", 0, 0, 0)
    coord.ignite("A", 0, 0, 0, 0, 0, 1, IP)
    inc = next(iter(coord.incidents))
    # ещё 1 кандидат в тот же incident — ок (итого 2), следующий отклонён
    assert coord.propose("A", IP, [{"x": 4, "y": 0, "z": 0, "incidentId": inc}]) == 1
    assert coord.propose("A", IP, [{"x": 8, "y": 0, "z": 0, "incidentId": inc}]) == 0


def test_set_config_clamps_and_returns_caps():
    coord, _ = make()
    caps = coord.set_config({"spread_chance": 5.0, "ext_range": 999, "burn_seconds": 30,
                             "max_inflight": 4, "unknown_key": 1})
    assert coord.cfg.spread_chance == 1.0      # кламп к [0,1]
    assert coord.cfg.ext_range == 60.0         # кламп к max
    assert coord.cfg.burn_seconds == 30.0
    assert coord.cfg.max_inflight == 4
    assert caps["spreadChance"] == 1.0 and caps["extRange"] == 60.0


def test_auto_burnout_after_burn_seconds():
    coord, clk = make(place_range=50, burn_seconds=5, worker_stale_s=100)
    connect(coord, "A", 0, 0, 0)
    coord.ignite("A", 1, 1, 0, 0, 0, 1, IP)
    job = coord.dispatch()[0][1]
    coord.job_done("A", job["id"], True, 3)
    cell = next(iter(coord.cells.values()))
    assert cell.state == STATE_BURNING
    clk.advance(6)            # > burn_seconds
    coord.tick()
    assert cell.state == STATE_EXTINGUISHING
    assert any(j.kind == KIND_REMOVE for j in coord.jobs.values())


def test_caps_expose_grid_and_range():
    coord, _ = make(grid=2.0, spread_min_heat=40, place_range=28)
    caps = coord.caps()
    assert caps["grid"] == 2.0 and caps["spreadMinHeat"] == 40
    assert caps["placeRange"] == 28 and caps["heatRamp"] == 20.0
