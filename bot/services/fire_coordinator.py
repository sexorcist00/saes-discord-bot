"""
FireCoordinator — координатор системы пожара ObjMapper (НЕ симулятор).

Идея системы: бэкенд НЕ считает, куда идёт огонь (у Python нет геометрии GTA). Эту
часть считают КЛИЕНТЫ рядом с очагом (raycast). Координатор лишь сводит результаты:
канонический реестр ячеек, дедуп через claim (первый побеждает), учёт heat и
жизненного цикла (простые счётчики), маршрутизация удаления, капы, кулдаун, GC.

Огонь = реальные серверные объекты (`/objects`) → их видят все игроки. Поэтому
координатору важно хранить `server_object_id` и `placed_by`: удалить объект по id
(`/dobjects <id>`) на Gambit может ТОЛЬКО тот, кто его поставил — значит remove
адресуется оригинальному placer'у; если он офлайн, удаление идёт ближайшему клиенту
как foreign-delete (клик, нужна близость).

Модуль самодостаточный и синхронный (без async/discord/БД внутри) — его дёргают
async-обработчики из api/server.py на одной asyncio-петле, гонок нет. Это же делает
его тривиально юнит-тестируемым.

Сетка: мир квантуется по grid (горизонталь) и grid_z (вертикаль); ключ ячейки —
(server_ip, gridKey). Несколько независимых пожаров различаются `incident_id`.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from bot.utils.logger import get_logger

logger = get_logger("services.fire")


# ── Состояния ячейки ────────────────────────────────────────────────────────
#  proposed     — заявлена клиентом, выдан claim, клиент сейчас ставит объект
#  burning      — объект стоит (есть server_object_id), heat растёт
#  extinguishing— heat<=0, ждёт удаления объекта
#  out          — объект удалён; помним до cooldown (чтобы не вспыхнуло сразу)
STATE_PROPOSED = "proposed"
STATE_BURNING = "burning"
STATE_EXTINGUISHING = "extinguishing"
STATE_OUT = "out"


@dataclass
class FireConfig:
    """Параметры координатора (берутся из bot.config, см. fire.* в config.yaml)."""
    grid: float = 2.0            # шаг сетки по горизонтали (м)
    grid_z: float = 2.0          # шаг сетки по вертикали (м)
    max_cells: int = 60          # кап активных ячеек на incident
    cooldown_s: float = 30.0     # потушенная ячейка не вспыхивает это время
    claim_ttl_s: float = 8.0     # claim протух → ячейка снимается, кто-то поставит заново
    merge_dist: float = 8.0      # поджог сливается с incident в этом радиусе
    heat_max: float = 100.0      # потолок heat
    heat_ramp_per_s: float = 20.0  # рост heat у burning (за сек)
    water_factor: float = 1.0    # множитель «единицы воды → минус heat»
    client_stale_s: float = 15.0  # клиент без sync дольше — считается офлайн
    remove_retry_s: float = 10.0  # remove не подтверждён → переназначить
    near_radius: float = 80.0    # радиус выдачи cells_near для оверлея (м)
    spread_min_heat: float = 40.0  # ниже этого heat ячейка ещё не разрешает спред


@dataclass
class Cell:
    id: str
    incident_id: str
    server_ip: str
    grid_key: str
    x: float
    y: float
    z: float
    nx: float
    ny: float
    nz: float
    state: str = STATE_PROPOSED
    heat: float = 0.0
    server_object_id: Optional[int] = None
    placed_by: Optional[str] = None      # user_id поставившего (для /dobjects <id>)
    assigned_to: Optional[str] = None    # кому выдан claim (placing)
    claim_deadline: float = 0.0          # monotonic; protух claim
    remove_assigned_to: Optional[str] = None
    remove_deadline: float = 0.0
    created_ts: float = 0.0
    ignited_ts: float = 0.0
    out_ts: float = 0.0

    def to_near(self) -> dict:
        """Лёгкое представление для оверлея клиента (cells_near)."""
        return {
            "id": self.id,
            "incidentId": self.incident_id,
            "gridKey": self.grid_key,
            "x": self.x, "y": self.y, "z": self.z,
            "nx": self.nx, "ny": self.ny, "nz": self.nz,
            "state": self.state,
            "heat": round(self.heat, 1),
        }


@dataclass
class _Client:
    user_id: str
    nick: str
    x: float
    y: float
    z: float
    server_ip: str
    last_seen: float = 0.0  # monotonic


@dataclass
class Incident:
    id: str
    server_ip: str
    created_ts: float
    x: float  # точка возникновения (для merge/инфо)
    y: float
    z: float


class FireCoordinator:
    def __init__(self, config: Optional[FireConfig] = None, *, clock=None, monotonic=None):
        self.cfg = config or FireConfig()
        # Инъекция времени для детерминированных тестов.
        self._clock = clock or time.time
        self._mono = monotonic or time.monotonic

        # Реестр ячеек: (server_ip, grid_key) -> Cell. Ключ гарантирует дедуп.
        self.cells: Dict[Tuple[str, str], Cell] = {}
        # Инциденты: incident_id -> Incident.
        self.incidents: Dict[str, Incident] = {}
        # Онлайн-клиенты: user_id -> _Client (для «ближайшего» и foreign-delete).
        self.clients: Dict[str, _Client] = {}

    # ── Утилиты ──────────────────────────────────────────────────────────────

    def _grid_key(self, x: float, y: float, z: float) -> str:
        gx = math.floor(x / self.cfg.grid)
        gy = math.floor(y / self.cfg.grid)
        gz = math.floor(z / self.cfg.grid_z)
        return f"{gx}:{gy}:{gz}"

    def _client_online(self, c: _Client, now_m: float) -> bool:
        return (now_m - c.last_seen) <= self.cfg.client_stale_s

    def _nearest_client(self, server_ip: str, x: float, y: float, z: float,
                        now_m: float, exclude: Optional[str] = None) -> Optional[_Client]:
        best, best_d = None, None
        for c in self.clients.values():
            if c.server_ip != server_ip or c.user_id == exclude:
                continue
            if not self._client_online(c, now_m):
                continue
            d = (c.x - x) ** 2 + (c.y - y) ** 2 + (c.z - z) ** 2
            if best_d is None or d < best_d:
                best, best_d = c, d
        return best

    def _incident_cell_count(self, incident_id: str) -> int:
        return sum(1 for c in self.cells.values()
                   if c.incident_id == incident_id and c.state != STATE_OUT)

    def _pick_incident(self, server_ip: str, x: float, y: float, z: float) -> Incident:
        """Найти incident в merge-радиусе на этом сервере или создать новый."""
        md2 = self.cfg.merge_dist ** 2
        best, best_d = None, None
        for inc in self.incidents.values():
            if inc.server_ip != server_ip:
                continue
            d = (inc.x - x) ** 2 + (inc.y - y) ** 2 + (inc.z - z) ** 2
            if d <= md2 and (best_d is None or d < best_d):
                best, best_d = inc, d
        if best:
            return best
        inc = Incident(id=uuid.uuid4().hex[:12], server_ip=server_ip,
                       created_ts=self._clock(), x=x, y=y, z=z)
        self.incidents[inc.id] = inc
        return inc

    # ── Клиент-реестр ────────────────────────────────────────────────────────

    def update_client(self, user_id: str, nick: str, pos: dict, server_ip: str) -> None:
        self.clients[user_id] = _Client(
            user_id=user_id, nick=nick or "",
            x=float(pos.get("x", 0.0)), y=float(pos.get("y", 0.0)), z=float(pos.get("z", 0.0)),
            server_ip=server_ip, last_seen=self._mono(),
        )

    # ── Поджог ───────────────────────────────────────────────────────────────

    def ignite(self, user_id: str, x: float, y: float, z: float,
               nx: float, ny: float, nz: float, server_ip: str) -> dict:
        """
        Зарегистрировать первичный очаг. Создаёт ячейку в состоянии proposed,
        выдаёт claim самому поджигателю (он рядом — он и поставит объект). Возвращает
        {incidentId, gridKey} — клиент ставит /objects и репортит serverId в sync.
        """
        gk = self._grid_key(x, y, z)
        key = (server_ip, gk)
        existing = self.cells.get(key)
        if existing and existing.state != STATE_OUT:
            return {"ok": False, "error": "ALREADY_BURNING",
                    "incidentId": existing.incident_id, "gridKey": gk}

        inc = self._pick_incident(server_ip, x, y, z)
        now, now_m = self._clock(), self._mono()
        cell = Cell(
            id=uuid.uuid4().hex[:12], incident_id=inc.id, server_ip=server_ip,
            grid_key=gk, x=x, y=y, z=z, nx=nx, ny=ny, nz=nz,
            state=STATE_PROPOSED, assigned_to=user_id,
            claim_deadline=now_m + self.cfg.claim_ttl_s, created_ts=now,
        )
        self.cells[key] = cell
        logger.info("ignite user=%s incident=%s gk=%s @ %.1f,%.1f,%.1f (cells incident=%d)",
                    user_id, inc.id, gk, x, y, z, self._incident_cell_count(inc.id))
        return {"ok": True, "incidentId": inc.id, "gridKey": gk}

    # ── Основной канал клиента ───────────────────────────────────────────────

    def sync(self, user_id: str, nick: str, pos: dict, server_ip: str,
             server_name: str = "", *, claims=None, placed=None, failed=None,
             water=None, removed=None) -> dict:
        """
        Один round-trip: репорт позиции + результаты + заявки → гранты/деnaи/removes +
        ближние ячейки. Все списки опциональны.
        """
        claims = claims or []
        placed = placed or []
        failed = failed or []
        water = water or []
        removed = removed or []

        now, now_m = self._clock(), self._mono()
        self.update_client(user_id, nick, pos, server_ip)

        # 1) Подтверждённые постановки: ячейка становится burning.
        for p in placed:
            cell = self.cells.get((server_ip, str(p.get("gridKey"))))
            if not cell or cell.state != STATE_PROPOSED:
                continue
            if cell.assigned_to != user_id:
                continue  # не его claim — игнор
            cell.server_object_id = int(p["serverId"])
            cell.placed_by = user_id
            cell.state = STATE_BURNING
            cell.ignited_ts = now
            cell.assigned_to = None
            logger.info("burning gk=%s serverId=%s by=%s incident=%s",
                        cell.grid_key, cell.server_object_id, user_id, cell.incident_id)

        # 2) Неудачные постановки (нет поверхности/обрыв): снять ячейку в cooldown.
        for f in failed:
            cell = self.cells.get((server_ip, str(f.get("gridKey"))))
            if cell and cell.state == STATE_PROPOSED and cell.assigned_to == user_id:
                cell.state = STATE_OUT
                cell.out_ts = now
                logger.info("place-failed gk=%s by=%s (нет поверхности/далеко)", cell.grid_key, user_id)

        # 3) Вода: снижаем heat; heat<=0 → extinguishing.
        for w in water:
            cell = self._cell_by_id(server_ip, str(w.get("cellId")))
            if cell and cell.state == STATE_BURNING:
                cell.heat -= float(w.get("amount", 0.0)) * self.cfg.water_factor
                if cell.heat <= 0.0:
                    cell.heat = 0.0
                    cell.state = STATE_EXTINGUISHING
                    logger.info("extinguishing cell=%s gk=%s (heat<=0)", cell.id, cell.grid_key)

        # 4) Подтверждённые удаления.
        for cid in removed:
            cell = self._cell_by_id(server_ip, str(cid))
            if cell and cell.state == STATE_EXTINGUISHING:
                cell.state = STATE_OUT
                cell.out_ts = now
                cell.server_object_id = None
                cell.remove_assigned_to = None
                logger.info("out cell=%s gk=%s (removed by=%s)", cell.id, cell.grid_key, user_id)

        # 5) Claim-заявки от клиента (дедуп: первый побеждает).
        grants: List[str] = []
        denied: List[str] = []
        for cl in claims:
            gk = str(cl.get("gridKey"))
            key = (server_ip, gk)
            existing = self.cells.get(key)
            if existing and existing.state != STATE_OUT:
                denied.append(gk)            # уже горит/ставится — дубль
                continue
            if existing and existing.state == STATE_OUT \
                    and (now - existing.out_ts) < self.cfg.cooldown_s:
                denied.append(gk)            # cooldown после тушения
                continue
            inc_id = str(cl.get("incidentId") or "")
            if inc_id and self._incident_cell_count(inc_id) >= self.cfg.max_cells:
                denied.append(gk)            # кап incident
                continue
            cell = Cell(
                id=uuid.uuid4().hex[:12],
                incident_id=inc_id or self._pick_incident(
                    server_ip, float(cl["x"]), float(cl["y"]), float(cl["z"])).id,
                server_ip=server_ip, grid_key=gk,
                x=float(cl["x"]), y=float(cl["y"]), z=float(cl["z"]),
                nx=float(cl.get("nx", 0.0)), ny=float(cl.get("ny", 0.0)),
                nz=float(cl.get("nz", 1.0)),
                state=STATE_PROPOSED, assigned_to=user_id,
                claim_deadline=now_m + self.cfg.claim_ttl_s, created_ts=now,
            )
            self.cells[key] = cell
            grants.append(gk)

        # 6) Раздать удаления и собрать задания для ЭТОГО клиента.
        self._ensure_remove_dispatch(server_ip, now_m)
        removes = self._removes_for(user_id, server_ip)

        # 7) Ближние ячейки для оверлея + спред-решений клиента.
        cells_near = self._cells_near(server_ip, pos)

        return {
            "ok": True,
            "grants": grants,
            "denied": denied,
            "removes": removes,
            "cells_near": cells_near,
            # Клиент квантует кандидаты спреда той же сеткой → gridKey совпадают у всех.
            "caps": {
                "maxCells": self.cfg.max_cells,
                "grid": self.cfg.grid,
                "gridZ": self.cfg.grid_z,
                "spreadMinHeat": self.cfg.spread_min_heat,
                "heatMax": self.cfg.heat_max,
                "heatRamp": self.cfg.heat_ramp_per_s,
            },
        }

    # ── Удаление: маршрутизация ──────────────────────────────────────────────

    def _ensure_remove_dispatch(self, server_ip: str, now_m: float) -> None:
        """Каждой extinguishing-ячейке назначить исполнителя удаления."""
        for cell in self.cells.values():
            if cell.server_ip != server_ip or cell.state != STATE_EXTINGUISHING:
                continue
            # назначение ещё валидно?
            if cell.remove_assigned_to:
                c = self.clients.get(cell.remove_assigned_to)
                if c and self._client_online(c, now_m) and now_m < cell.remove_deadline:
                    continue
            # Предпочесть оригинального placer'а: только он может /dobjects <id> свой объект.
            target = None
            if cell.placed_by:
                pc = self.clients.get(cell.placed_by)
                if pc and self._client_online(pc, now_m):
                    target = pc
            # Иначе — ближайший онлайн-клиент (foreign-delete кликом, нужна близость).
            if target is None:
                target = self._nearest_client(server_ip, cell.x, cell.y, cell.z, now_m)
            if target:
                cell.remove_assigned_to = target.user_id
                cell.remove_deadline = now_m + self.cfg.remove_retry_s
                mode = "own" if cell.placed_by == target.user_id else "foreign"
                logger.info("remove-dispatch cell=%s serverId=%s → user=%s mode=%s (placer=%s)",
                            cell.id, cell.server_object_id, target.user_id, mode, cell.placed_by)

    def _removes_for(self, user_id: str, server_ip: str) -> List[dict]:
        out = []
        for cell in self.cells.values():
            if (cell.server_ip == server_ip and cell.state == STATE_EXTINGUISHING
                    and cell.remove_assigned_to == user_id and cell.server_object_id is not None):
                mode = "own" if cell.placed_by == user_id else "foreign"
                out.append({"cellId": cell.id, "serverId": cell.server_object_id,
                            "mode": mode, "x": cell.x, "y": cell.y, "z": cell.z})
        return out

    # ── Жизненный цикл (фоновый tick) ────────────────────────────────────────

    def tick(self) -> None:
        """Лёгкий тик: рост heat, протухшие claim'ы, чистка out, GC висяков."""
        now, now_m = self._clock(), self._mono()
        dead_keys = []
        for key, cell in self.cells.items():
            if cell.state == STATE_BURNING:
                if cell.heat < self.cfg.heat_max:
                    cell.heat = min(self.cfg.heat_max,
                                    cell.heat + self.cfg.heat_ramp_per_s * self._tick_dt())
            elif cell.state == STATE_PROPOSED:
                # claim протух — поставить никто не успел → освободить ячейку.
                if now_m > cell.claim_deadline:
                    dead_keys.append(key)
            elif cell.state == STATE_OUT:
                # после cooldown окончательно забываем (gridKey освобождается).
                if (now - cell.out_ts) > self.cfg.cooldown_s:
                    dead_keys.append(key)
        for key in dead_keys:
            self.cells.pop(key, None)
        self._ensure_remove_dispatch_all(now_m)
        self._gc_incidents()

    def _ensure_remove_dispatch_all(self, now_m: float) -> None:
        ips = {c.server_ip for c in self.cells.values()}
        for ip in ips:
            self._ensure_remove_dispatch(ip, now_m)

    def _gc_incidents(self) -> None:
        """Удалить инциденты без живых ячеек."""
        alive = {c.incident_id for c in self.cells.values() if c.state != STATE_OUT}
        for iid in [i for i in self.incidents if i not in alive]:
            self.incidents.pop(iid, None)

    # фиксированный шаг для расчёта heat (реальный планировщик зовёт tick() раз/сек)
    def _tick_dt(self) -> float:
        return 1.0

    # ── Доступ/админ ─────────────────────────────────────────────────────────

    def _cell_by_id(self, server_ip: str, cell_id: str) -> Optional[Cell]:
        for cell in self.cells.values():
            if cell.server_ip == server_ip and cell.id == cell_id:
                return cell
        return None

    def _cells_near(self, server_ip: str, pos: dict) -> List[dict]:
        px, py, pz = float(pos.get("x", 0)), float(pos.get("y", 0)), float(pos.get("z", 0))
        r2 = self.cfg.near_radius ** 2
        out = []
        for cell in self.cells.values():
            if cell.server_ip != server_ip or cell.state == STATE_OUT:
                continue
            d = (cell.x - px) ** 2 + (cell.y - py) ** 2 + (cell.z - pz) ** 2
            if d <= r2:
                out.append(cell.to_near())
        return out

    def snapshot(self, server_ip: Optional[str] = None) -> dict:
        """Состояние для админ/дебага."""
        cells = [c for c in self.cells.values()
                 if server_ip is None or c.server_ip == server_ip]
        by_state: Dict[str, int] = {}
        for c in cells:
            by_state[c.state] = by_state.get(c.state, 0) + 1
        return {
            "incidents": len([i for i in self.incidents.values()
                              if server_ip is None or i.server_ip == server_ip]),
            "cells": len(cells),
            "by_state": by_state,
            "clients": len(self.clients),
        }

    def wipe(self, server_ip: Optional[str] = None) -> int:
        """Жёсткий сброс: ПОЛНОСТЬЮ выбросить ячейки из реестра (исчезают из cells_near,
        спред прекращается, синих extinguishing-хвостов не остаётся). Удаление самих
        серверных объектов делает клиент (/dobjects). Возвращает число снятых ячеек."""
        keys = [k for k, c in self.cells.items()
                if server_ip is None or c.server_ip == server_ip]
        for k in keys:
            self.cells.pop(k, None)
        self._gc_incidents()
        logger.info("fire wipe: снято %d ячеек (server_ip=%s)", len(keys), server_ip)
        return len(keys)

    def reset(self, server_ip: Optional[str] = None) -> int:
        """Сбросить пожар(ы). Возвращает число снятых ячеек (для уборки клиентами —
        они увидят пропажу в cells_near; реальное удаление объектов = отдельный проход
        через extinguishing). v1: помечаем всё extinguishing, чтобы выдать removes."""
        n = 0
        now = self._clock()
        for cell in self.cells.values():
            if server_ip and cell.server_ip != server_ip:
                continue
            if cell.state == STATE_BURNING and cell.server_object_id is not None:
                cell.state = STATE_EXTINGUISHING
                cell.heat = 0.0
                n += 1
            elif cell.state == STATE_PROPOSED:
                cell.state = STATE_OUT
                cell.out_ts = now
                n += 1
        return n
