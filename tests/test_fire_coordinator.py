"""Юнит-тесты FireCoordinator — чистая логика, без игры/сети/БД."""

import pytest

from bot.services.fire_coordinator import (
    FireCoordinator, FireConfig,
    STATE_PROPOSED, STATE_BURNING, STATE_EXTINGUISHING, STATE_OUT,
)


class FakeClock:
    """Управляемые часы: и wall-clock, и monotonic двигаются вместе."""
    def __init__(self):
        self.t = 1000.0

    def advance(self, dt):
        self.t += dt

    def __call__(self):
        return self.t


IP = "1.2.3.4:7777"


def make_coord(**cfg):
    clk = FakeClock()
    coord = FireCoordinator(FireConfig(**cfg), clock=clk, monotonic=clk)
    return coord, clk


def test_ignite_creates_incident_and_proposed_cell():
    coord, _ = make_coord()
    res = coord.ignite("u1", 10.0, 20.0, 3.0, 0, 0, 1, IP)
    assert res["ok"] is True
    assert res["incidentId"] in coord.incidents
    assert len(coord.cells) == 1
    cell = next(iter(coord.cells.values()))
    assert cell.state == STATE_PROPOSED
    assert cell.assigned_to == "u1"


def test_placed_report_turns_cell_burning():
    coord, _ = make_coord()
    res = coord.ignite("u1", 10.0, 20.0, 3.0, 0, 0, 1, IP)
    gk = res["gridKey"]
    out = coord.sync("u1", "Nick", {"x": 10, "y": 20, "z": 3}, IP,
                     placed=[{"gridKey": gk, "serverId": 555}])
    assert out["ok"]
    cell = next(iter(coord.cells.values()))
    assert cell.state == STATE_BURNING
    assert cell.server_object_id == 555
    assert cell.placed_by == "u1"


def test_claim_dedup_first_wins():
    coord, _ = make_coord()
    pos = {"x": 50, "y": 50, "z": 2}
    claim = {"gridKey": "25:25:1", "x": 50, "y": 50, "z": 2, "nz": 1}
    r1 = coord.sync("u1", "A", pos, IP, claims=[claim])
    r2 = coord.sync("u2", "B", pos, IP, claims=[claim])
    assert "25:25:1" in r1["grants"]
    assert "25:25:1" in r2["denied"]
    assert "25:25:1" not in r2["grants"]


def test_failed_placement_frees_cell_into_cooldown():
    coord, clk = make_coord(cooldown_s=30)
    res = coord.ignite("u1", 10, 20, 3, 0, 0, 1, IP)
    gk = res["gridKey"]
    coord.sync("u1", "A", {"x": 10, "y": 20, "z": 3}, IP, failed=[{"gridKey": gk}])
    cell = next(iter(coord.cells.values()))
    assert cell.state == STATE_OUT
    # В cooldown повторный claim того же ключа отклоняется.
    r = coord.sync("u1", "A", {"x": 10, "y": 20, "z": 3}, IP,
                   claims=[{"gridKey": gk, "x": 10, "y": 20, "z": 3, "nz": 1}])
    assert gk in r["denied"]


def test_water_drives_to_extinguishing():
    coord, _ = make_coord(water_factor=1.0)
    res = coord.ignite("u1", 0, 0, 0, 0, 0, 1, IP)
    gk = res["gridKey"]
    coord.sync("u1", "A", {"x": 0, "y": 0, "z": 0}, IP,
               placed=[{"gridKey": gk, "serverId": 7}])
    cell = next(iter(coord.cells.values()))
    cell.heat = 50.0
    coord.sync("u1", "A", {"x": 0, "y": 0, "z": 0}, IP,
               water=[{"cellId": cell.id, "amount": 60}])
    assert cell.state == STATE_EXTINGUISHING
    assert cell.heat == 0.0


def test_remove_routed_to_original_placer():
    coord, _ = make_coord()
    res = coord.ignite("placer", 0, 0, 0, 0, 0, 1, IP)
    gk = res["gridKey"]
    coord.sync("placer", "P", {"x": 0, "y": 0, "z": 0}, IP,
               placed=[{"gridKey": gk, "serverId": 99}])
    cell = next(iter(coord.cells.values()))
    cell.state = STATE_EXTINGUISHING
    cell.heat = 0.0
    # Другой клиент рядом, но remove должен уйти placer'у (id-delete только у него).
    coord.sync("bystander", "B", {"x": 1, "y": 1, "z": 0}, IP)
    out = coord.sync("placer", "P", {"x": 0, "y": 0, "z": 0}, IP)
    ids = [r["serverId"] for r in out["removes"]]
    assert 99 in ids
    assert out["removes"][0]["mode"] == "own"


def test_remove_fallback_to_nearest_when_placer_offline():
    coord, clk = make_coord(client_stale_s=15)
    res = coord.ignite("placer", 0, 0, 0, 0, 0, 1, IP)
    gk = res["gridKey"]
    coord.sync("placer", "P", {"x": 0, "y": 0, "z": 0}, IP,
               placed=[{"gridKey": gk, "serverId": 42}])
    cell = next(iter(coord.cells.values()))
    cell.state = STATE_EXTINGUISHING
    cell.heat = 0.0
    # placer уходит офлайн (время > client_stale_s без sync), приходит сосед.
    clk.advance(20)
    out = coord.sync("near", "N", {"x": 1, "y": 1, "z": 0}, IP)
    ids = [r["serverId"] for r in out["removes"]]
    assert 42 in ids
    assert out["removes"][0]["mode"] == "foreign"


def test_removed_confirmation_marks_out():
    coord, _ = make_coord()
    res = coord.ignite("u1", 0, 0, 0, 0, 0, 1, IP)
    gk = res["gridKey"]
    coord.sync("u1", "A", {"x": 0, "y": 0, "z": 0}, IP,
               placed=[{"gridKey": gk, "serverId": 9}])
    cell = next(iter(coord.cells.values()))
    cell.state = STATE_EXTINGUISHING
    cell.heat = 0.0
    coord.sync("u1", "A", {"x": 0, "y": 0, "z": 0}, IP, removed=[cell.id])
    assert cell.state == STATE_OUT
    assert cell.server_object_id is None


def test_tick_ramps_heat_and_expires_claims():
    coord, clk = make_coord(heat_ramp_per_s=20, heat_max=100, claim_ttl_s=8)
    # burning ячейка — heat растёт.
    res = coord.ignite("u1", 0, 0, 0, 0, 0, 1, IP)
    coord.sync("u1", "A", {"x": 0, "y": 0, "z": 0}, IP,
               placed=[{"gridKey": res["gridKey"], "serverId": 1}])
    cell = next(iter(coord.cells.values()))
    coord.tick()
    assert cell.heat == 20.0
    # proposed ячейка с протухшим claim — снимается тиком.
    coord.sync("u2", "B", {"x": 100, "y": 100, "z": 0}, IP,
               claims=[{"gridKey": "50:50:0", "x": 100, "y": 100, "z": 0, "nz": 1}])
    assert (IP, "50:50:0") in coord.cells
    clk.advance(10)  # > claim_ttl_s
    coord.tick()
    assert (IP, "50:50:0") not in coord.cells


def test_out_cell_cleared_after_cooldown():
    coord, clk = make_coord(cooldown_s=30)
    res = coord.ignite("u1", 0, 0, 0, 0, 0, 1, IP)
    coord.sync("u1", "A", {"x": 0, "y": 0, "z": 0}, IP, failed=[{"gridKey": res["gridKey"]}])
    assert len(coord.cells) == 1
    clk.advance(31)
    coord.tick()
    assert len(coord.cells) == 0
    assert len(coord.incidents) == 0  # GC инцидента без живых ячеек


def test_ignite_merges_into_nearby_incident():
    coord, _ = make_coord(merge_dist=8.0)
    r1 = coord.ignite("u1", 0, 0, 0, 0, 0, 1, IP)
    r2 = coord.ignite("u2", 3, 0, 0, 0, 0, 1, IP)  # в пределах merge_dist
    assert r1["incidentId"] == r2["incidentId"]
    assert len(coord.incidents) == 1
    # далеко — новый incident
    r3 = coord.ignite("u3", 100, 100, 0, 0, 0, 1, IP)
    assert r3["incidentId"] != r1["incidentId"]
    assert len(coord.incidents) == 2


def test_cells_near_radius_filtering():
    coord, _ = make_coord(near_radius=50)
    coord.ignite("u1", 0, 0, 0, 0, 0, 1, IP)
    coord.ignite("u1", 200, 0, 0, 0, 0, 1, IP)  # далеко
    out = coord.sync("u1", "A", {"x": 0, "y": 0, "z": 0}, IP)
    assert len(out["cells_near"]) == 1


def test_max_cells_cap_denies_claim():
    coord, _ = make_coord(max_cells=2)
    res = coord.ignite("u1", 0, 0, 0, 0, 0, 1, IP)
    inc = res["incidentId"]
    # две заявки в тот же incident — обе ок (ignite-ячейка proposed уже считается)
    pos = {"x": 0, "y": 0, "z": 0}
    coord.sync("u1", "A", pos, IP, claims=[
        {"gridKey": "10:0:0", "x": 20, "y": 0, "z": 0, "nz": 1, "incidentId": inc},
    ])
    # incident уже содержит 2 ячейки (ignite + 1 claim) → следующая отклонена
    r = coord.sync("u1", "A", pos, IP, claims=[
        {"gridKey": "20:0:0", "x": 40, "y": 0, "z": 0, "nz": 1, "incidentId": inc},
    ])
    assert "20:0:0" in r["denied"]
