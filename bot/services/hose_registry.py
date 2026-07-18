"""Реестр рукавных линий ObjMapper (HOSE-LINE.md, Фаза 3 + синк физики/воды).

Сервер синкает ТОПОЛОГИЮ и ДИСКРЕТНОЕ состояние линии (класс, кран/давление,
скатка): payload hose_create + мутации hose_water/hose_stow — всё это хранится
и отдаётся в снапшоте поздно вошедшим (hose_state после welcome). Форма провиса
(hose_shape, кадры узлов от владельца) и сбивание струёй (hose_knock) — чистый
ретранслятор с rate-limit'ом, реестр их НЕ хранит: владелец линии авторитетен
по своей физике, сервер не считает и не запоминает узлы.
TTL-гарантия прежняя: дисконнект владельца сносит его линии у всех (hose_remove).
"""

import time
from typing import Dict, List, Optional


MAX_PER_OWNER = 4      # линий на владельца (анти-флуд)
MAX_PER_SERVER = 64    # линий на игровой сервер

_LEN_MIN, _LEN_MAX = 5.0, 100.0
_RAD_MIN, _RAD_MAX = 0.02, 0.30

_CLASSES = ("attack", "supply")
_AGENTS = ("water", "foam")

SHAPE_MAX_NODES = 128            # узлов в кадре формы (клиент шлёт ~40)
SHAPE_MIN_INTERVAL = 0.08        # с: min-интервал кадров формы на линию (клиент шлёт 8 Гц)
KNOCK_MIN_INTERVAL = 0.25        # с: min-интервал hose_knock на отправителя


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


def sanitize_water(w) -> Optional[dict]:
    """Состояние крана линии: {on, p, agent}. None = мусор (отвергнуть)."""
    if not isinstance(w, dict):
        return None
    try:
        p = _clamp(float(w.get("p") or 0.0), 0.0, 1.0)
    except (TypeError, ValueError):
        return None
    agent = str(w.get("agent") or "water")
    if agent not in _AGENTS:
        agent = "water"
    return {"on": bool(w.get("on")), "p": round(p, 2), "agent": agent}


def sanitize_stow(s) -> object:
    """Скатка: False = размотан, dict = смотан (в руке/на земле), None = мусор."""
    if s is False or s is None:
        return False
    if not isinstance(s, dict):
        return None
    mode = str(s.get("mode") or "")
    if mode not in ("hand", "ground"):
        return None
    try:
        out = {k: _clamp(float(s.get(k) or 0.0), -20000.0, 20000.0)
               for k in ("x", "y", "z", "ang")}
    except (TypeError, ValueError):
        return None
    out["mode"] = mode
    if s.get("noz"):
        out["noz"] = True
    return out


def sanitize_shape(data: dict) -> Optional[dict]:
    """Кадр формы (hose_shape) от владельца: чистый ретранслятор, НЕ хранится.
    Узлы — int-дельты в сантиметрах от якоря (ax,ay,az). None = отвергнуто."""
    if not isinstance(data, dict):
        return None
    hose_id = str(data.get("id") or "")
    n = data.get("n")
    if not hose_id or len(hose_id) > 64 or not isinstance(n, list):
        return None
    if len(n) % 3 != 0 or len(n) > SHAPE_MAX_NODES * 3:
        return None
    try:
        nodes = [int(_clamp(float(v), -2000000.0, 2000000.0)) for v in n]
        out = {
            "id": hose_id,
            "k": int(data.get("k") or 0),
            "ax": round(_clamp(float(data.get("ax") or 0.0), -20000.0, 20000.0), 2),
            "ay": round(_clamp(float(data.get("ay") or 0.0), -20000.0, 20000.0), 2),
            "az": round(_clamp(float(data.get("az") or 0.0), -20000.0, 20000.0), 2),
            "n": nodes,
            "f": round(_clamp(float(data.get("f") or 0.0), 0.0, 1.0), 2),
        }
    except (TypeError, ValueError):
        return None
    m = data.get("m")
    if isinstance(m, dict):
        try:
            out["m"] = {k: round(_clamp(float(m.get(k) or 0.0), -20000.0, 20000.0), 2)
                        for k in ("x", "y", "z", "dx", "dy", "dz")}
        except (TypeError, ValueError):
            return None
    if data.get("fire"):
        out["fire"] = True
    if data.get("loose"):
        out["loose"] = True                # летящий ствол (хлыст): remote ведёт flyObject
    return out


def sanitize_knock(data: dict) -> Optional[dict]:
    """Сбивание струёй (hose_knock): pid жертвы + импульс. Чистый ретранслятор."""
    if not isinstance(data, dict):
        return None
    try:
        pid = int(data.get("pid"))
        out = {"pid": pid,
               "vx": round(_clamp(float(data.get("vx") or 0.0), -50.0, 50.0), 3),
               "vy": round(_clamp(float(data.get("vy") or 0.0), -50.0, 50.0), 3),
               "vz": round(_clamp(float(data.get("vz") or 0.0), -50.0, 50.0), 3)}
    except (TypeError, ValueError):
        return None
    if not 0 <= pid <= 1004:
        return None
    return out


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
    cls = str(data.get("class") or "attack")
    out["class"] = cls if cls in _CLASSES else "attack"
    w = sanitize_water(data.get("w"))
    if w is not None:
        out["w"] = w
    stow = sanitize_stow(data.get("stow"))
    if isinstance(stow, dict):                 # линия создана уже смотанной (реанонс)
        out["stow"] = stow
    return out


class HoseRegistry:
    """Хранилище активных линий: id → {payload, owner_uid, server_ip}."""

    def __init__(self):
        self.hoses: Dict[str, dict] = {}
        self._shape_at: Dict[str, float] = {}   # id линии → время последнего кадра формы
        self._knock_at: Dict[str, float] = {}   # uid отправителя → время последнего knock

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

    def set_water(self, owner_uid: str, data: dict) -> Optional[dict]:
        """Кран/давление/агент линии (hose_water, только владелец): обновить payload
        (снапшот увидит текущее состояние воды). → {id, w, server_ip} или None."""
        hose_id = str((data or {}).get("id") or "")
        h = self.hoses.get(hose_id)
        if h is None or h["owner_uid"] != owner_uid:
            return None
        w = sanitize_water(data)
        if w is None:
            return None
        h["payload"]["w"] = w
        return {"id": hose_id, "w": w, "server_ip": h["server_ip"]}

    def set_stow(self, owner_uid: str, data: dict) -> Optional[dict]:
        """Скатка (hose_stow, только владелец): смотан (в руке/на земле) или размотан.
        → {id, stow, server_ip} или None. stow=False — размотан (deploy)."""
        hose_id = str((data or {}).get("id") or "")
        h = self.hoses.get(hose_id)
        if h is None or h["owner_uid"] != owner_uid:
            return None
        stow = sanitize_stow((data or {}).get("stow"))
        if stow is None:
            return None
        if stow is False:
            h["payload"].pop("stow", None)
        else:
            h["payload"]["stow"] = stow
        return {"id": hose_id, "stow": stow, "server_ip": h["server_ip"]}

    def shape_ok(self, owner_uid: str, data: dict, now: Optional[float] = None) -> Optional[dict]:
        """Кадр формы (hose_shape): владелец + rate-limit SHAPE_MIN_INTERVAL на линию.
        НЕ хранится (владелец авторитетен, сервер узлы не помнит). → чистый кадр
        c server_ip для broadcast или None (чужая линия/флуд/мусор — молча дропнуть)."""
        clean = sanitize_shape(data)
        if clean is None:
            return None
        h = self.hoses.get(clean["id"])
        if h is None or h["owner_uid"] != owner_uid:
            return None
        now = time.monotonic() if now is None else now
        last = self._shape_at.get(clean["id"])
        if last is not None and now - last < SHAPE_MIN_INTERVAL:
            return None
        self._shape_at[clean["id"]] = now
        clean["server_ip"] = h["server_ip"]
        return clean

    def knock_ok(self, sender_uid: str, data: dict, now: Optional[float] = None) -> Optional[dict]:
        """Сбивание струёй (hose_knock): rate-limit KNOCK_MIN_INTERVAL на отправителя.
        Чистый ретранслятор (server_ip берёт вызывающий из Worker отправителя)."""
        clean = sanitize_knock(data)
        if clean is None:
            return None
        now = time.monotonic() if now is None else now
        last = self._knock_at.get(sender_uid)
        if last is not None and now - last < KNOCK_MIN_INTERVAL:
            return None
        self._knock_at[sender_uid] = now
        return clean

    def remove(self, hose_id: str, owner_uid: str) -> Optional[str]:
        """Удалить линию (только владелец). → server_ip для broadcast или None."""
        h = self.hoses.get(hose_id)
        if h is None or h["owner_uid"] != owner_uid:
            return None
        del self.hoses[hose_id]
        self._shape_at.pop(hose_id, None)
        return h["server_ip"]

    def remove_all_for(self, owner_uid: str) -> List[dict]:
        """Снести все линии владельца (дисконнект). → [{id, server_ip}] для broadcast."""
        gone = [{"id": hid, "server_ip": h["server_ip"]}
                for hid, h in self.hoses.items() if h["owner_uid"] == owner_uid]
        for g in gone:
            del self.hoses[g["id"]]
            self._shape_at.pop(g["id"], None)
        self._knock_at.pop(owner_uid, None)
        return gone

    def snapshot(self, server_ip: str) -> List[dict]:
        """Все линии игрового сервера (для hose_state поздно вошедшему)."""
        return [h["payload"] for h in self.hoses.values() if h["server_ip"] == server_ip]
