"""Реестр рукавных линий ObjMapper (HOSE-LINE.md, Фаза 3).

Сервер синкает только ТОПОЛОГИЮ: payload hose_create (пины концов: SAMP id машины
и игрока-держателя) — физика провиса у каждого клиента локальная, позиции узлов по
сети не ходят. Реестр нужен для снапшота поздно вошедшим (hose_state после welcome)
и для TTL-гарантии: дисконнект владельца сносит его линии у всех (hose_remove).
"""

from typing import Dict, List, Optional


MAX_PER_OWNER = 4      # линий на владельца (анти-флуд)
MAX_PER_SERVER = 64    # линий на игровой сервер

_LEN_MIN, _LEN_MAX = 5.0, 100.0
_RAD_MIN, _RAD_MAX = 0.02, 0.30


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def sanitize_drop(d) -> object:
    """Поза брошенного ствола: False = в руке, dict = лежит, None = мусор (отвергнуть)."""
    if d is False or d is None:
        return False
    if not isinstance(d, dict):
        return None
    try:
        return {k: _clamp(float(d.get(k) or 0.0), -20000.0, 20000.0)
                for k in ("x", "y", "z", "dx", "dy", "dz")}
    except (TypeError, ValueError):
        return None


def sanitize(data: dict) -> Optional[dict]:
    """Проверить/нормализовать payload hose_create от клиента. None = отвергнуто."""
    if not isinstance(data, dict):
        return None
    hose_id = str(data.get("id") or "")
    if not hose_id or len(hose_id) > 64:
        return None
    a, b = data.get("a"), data.get("b")
    if not isinstance(a, dict) or not isinstance(b, dict):
        return None
    try:
        veh_id = int(a.get("vehId"))
        player_id = int(b.get("playerId"))
        length_m = _clamp(float(data.get("lengthM") or 20.0), _LEN_MIN, _LEN_MAX)
        radius = _clamp(float(data.get("radius") or 0.08), _RAD_MIN, _RAD_MAX)
    except (TypeError, ValueError):
        return None
    if not (0 <= veh_id <= 4000 and 0 <= player_id <= 1004):
        return None
    connector = str(a.get("connector") or "rear")[:16]
    out = {
        "id": hose_id,
        "owner": str(data.get("owner") or "")[:32],
        "a": {"vehId": veh_id, "connector": connector},
        "b": {"playerId": player_id},
        "lengthM": length_m,
        "radius": radius,
    }
    model = a.get("model")
    if isinstance(model, (int, float)):
        out["a"]["model"] = int(model)
    drop = sanitize_drop(b.get("drop"))
    if isinstance(drop, dict):                 # линия создана уже с лежащим стволом (реанонс)
        out["b"]["drop"] = drop
    return out


class HoseRegistry:
    """Хранилище активных линий: id → {payload, owner_uid, server_ip}."""

    def __init__(self):
        self.hoses: Dict[str, dict] = {}

    def _count_for(self, owner_uid: str, server_ip: str) -> tuple:
        own = srv = 0
        for h in self.hoses.values():
            if h["owner_uid"] == owner_uid:
                own += 1
            if h["server_ip"] == server_ip:
                srv += 1
        return own, srv

    def upsert(self, owner_uid: str, server_ip: str, data: dict) -> Optional[dict]:
        """Создать/обновить линию владельца. → чистый payload для broadcast или None."""
        clean = sanitize(data)
        if clean is None or not server_ip:
            return None
        existing = self.hoses.get(clean["id"])
        if existing is not None and existing["owner_uid"] != owner_uid:
            return None                      # чужой id не перехватить
        if existing is None:
            own, srv = self._count_for(owner_uid, server_ip)
            if own >= MAX_PER_OWNER or srv >= MAX_PER_SERVER:
                return None
        self.hoses[clean["id"]] = {"payload": clean, "owner_uid": owner_uid,
                                   "server_ip": server_ip}
        return clean

    def attach(self, owner_uid: str, data: dict) -> Optional[dict]:
        """Бросок/поднятие ствола (hose_attach, только владелец): обновить payload
        (снапшот поздно вошедшим увидит лежащий ствол). → {id, drop, server_ip} или None."""
        hose_id = str(data.get("id") or "")
        h = self.hoses.get(hose_id)
        if h is None or h["owner_uid"] != owner_uid:
            return None
        drop = sanitize_drop(data.get("drop"))
        if drop is None:
            return None
        b = h["payload"]["b"]
        if drop is False:
            b.pop("drop", None)
        else:
            b["drop"] = drop
        return {"id": hose_id, "drop": drop, "server_ip": h["server_ip"]}

    def remove(self, hose_id: str, owner_uid: str) -> Optional[str]:
        """Удалить линию (только владелец). → server_ip для broadcast или None."""
        h = self.hoses.get(hose_id)
        if h is None or h["owner_uid"] != owner_uid:
            return None
        del self.hoses[hose_id]
        return h["server_ip"]

    def remove_all_for(self, owner_uid: str) -> List[dict]:
        """Снести все линии владельца (дисконнект). → [{id, server_ip}] для broadcast."""
        gone = [{"id": hid, "server_ip": h["server_ip"]}
                for hid, h in self.hoses.items() if h["owner_uid"] == owner_uid]
        for g in gone:
            del self.hoses[g["id"]]
        return gone

    def snapshot(self, server_ip: str) -> List[dict]:
        """Все линии игрового сервера (для hose_state поздно вошедшему)."""
        return [h["payload"] for h in self.hoses.values() if h["server_ip"] == server_ip]
