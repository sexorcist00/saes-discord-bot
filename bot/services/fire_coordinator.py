"""
FireCoordinator — координатор пожара ObjMapper: воркер-пул + очередь заданий.

Модель (после перехода на WebSocket):
  • Геометрию знает только клиент → распространение он же и считает (raycast),
    присылая КАНДИДАТЫ (propose). Бэкенд НЕ симулирует — он КООРДИНИРУЕТ.
  • Бэкенд владеет ВСЕМИ заданиями (place из кандидатов/поджога + remove потушенных)
    и раздаёт их наименее загруженному in-range воркеру (балансировка нагрузки),
    переназначая при дисконнекте/таймауте. Это даёт распределение работы по всем
    онлайн-клиентам, а не «кто рядом — тот один и пашет».
  • Огонь = реальные серверные объекты (/objects) → их видят все. Удаляет объект по
    id (/dobjects <id>) ТОЛЬКО его placer → remove-задание адресуется ему (own); если
    он офлайн — ближайшему воркеру как foreign (клик-делит, нужна близость).

Транспорт (WebSocket) живёт в api/server.py; здесь — чистая, синхронная,
юнит-тестируемая логика. dispatch() возвращает назначения (user_id → job), их
рассылает сервер по сокетам. Партиция по server_ip; пожары различаются incident_id.
Сетка: (server_ip, gridKey), grid из конфига.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from bot.utils.logger import get_logger

logger = get_logger("services.fire")


# ── Состояния ячейки ────────────────────────────────────────────────────────
STATE_PROPOSED = "proposed"        # ячейка заявлена; есть place-задание (ждёт/в работе)
STATE_BURNING = "burning"          # объект стоит, heat растёт
STATE_EXTINGUISHING = "extinguishing"  # heat<=0; есть remove-задание
STATE_OUT = "out"                  # объект удалён; помним до cooldown

KIND_PLACE = "place"
KIND_REMOVE = "remove"


@dataclass
class FireConfig:
    grid: float = 2.0
    grid_z: float = 2.0
    max_cells: int = 60            # кап активных ячеек на incident
    cooldown_s: float = 30.0       # потушенная ячейка не вспыхивает это время
    merge_dist: float = 8.0        # поджог сливается с incident в этом радиусе
    heat_max: float = 100.0
    heat_ramp_per_s: float = 20.0  # рост heat у burning (за сек)
    water_factor: float = 1.0
    spread_min_heat: float = 40.0  # клиент спредит только очаги с heat выше этого
    # Воркер-пул / задания:
    place_range: float = 28.0      # макс. дистанция воркер→цель для постановки (лимит /objects)
    max_inflight: int = 2          # сколько заданий разом на воркера (заставляет распределять)
    job_timeout_s: float = 10.0    # задание не подтверждено → переназначить
    worker_stale_s: float = 12.0   # воркер без вестей дольше — офлайн (реквью его заданий)
    # ── Настраиваемые админом (live через меню; уходят клиентам в caps) ──
    spread_interval: float = 2.5   # сек между расчётами спреда на клиенте
    spread_chance: float = 0.55    # вероятность кандидата в сторону
    spread_max_per_tick: int = 6   # лимит новых кандидатов за расчёт
    burn_seconds: float = 0.0      # автозатухание очага (0 = горит до тушения)
    wind_x: float = 0.0            # вектор ветра (нормируется на клиенте)
    wind_y: float = 0.0
    wind_strength: float = 0.0     # 0..1 — сила направленного биаса
    slope_bias: float = 0.5        # вклад «вверх по склону быстрее» (0 = выкл)
    fuel_bias: float = 0.5         # вклад тяги к горючим поверхностям (0 = выкл)
    ext_water_per_sec: float = 60.0  # скорость воды огнетушителя
    ext_range: float = 12.0          # дальность струи (м)
    ext_radius: float = 3.0          # радиус захвата у точки удара (м)
    paused: bool = False             # админ-«стоп»: клиенты не предлагают новых очагов
    # Горючие типы ПОВЕРХНОСТЕЙ SA (surfaceType из колпоинта raycast): дерево/трава/листва.
    # Клиент по ним повышает вес кандидата (материал надёжнее перечня моделей). Дефолт ПУСТ —
    # populate'ится после калибровки: /firedebug покажет surfaceType кандидатов в игре.
    fuel_surfaces: list = field(default_factory=list)


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
    placed_by: Optional[str] = None
    created_ts: float = 0.0
    burning_ts: float = 0.0
    out_ts: float = 0.0

    def to_near(self) -> dict:
        return {
            "id": self.id, "incidentId": self.incident_id, "gridKey": self.grid_key,
            "x": self.x, "y": self.y, "z": self.z,
            "nx": self.nx, "ny": self.ny, "nz": self.nz,
            "state": self.state, "heat": round(self.heat, 1),
        }


@dataclass
class Job:
    id: str
    kind: str                 # place | remove
    server_ip: str
    grid_key: str             # ключ ячейки (server_ip, grid_key)
    x: float
    y: float
    z: float
    nx: float = 0.0
    ny: float = 0.0
    nz: float = 1.0
    incident_id: str = ""
    server_object_id: Optional[int] = None   # для remove
    prefer: Optional[str] = None             # placer (для remove own)
    assigned_to: Optional[str] = None
    dispatched_m: float = 0.0

    def payload(self) -> dict:
        d = {
            "id": self.id, "kind": self.kind, "gridKey": self.grid_key,
            "x": self.x, "y": self.y, "z": self.z,
            "nx": self.nx, "ny": self.ny, "nz": self.nz,
            "incidentId": self.incident_id,
        }
        if self.kind == KIND_REMOVE:
            d["serverId"] = self.server_object_id
            d["mode"] = "own" if self.assigned_to and self.assigned_to == self.prefer else "foreign"
        return d


@dataclass
class Worker:
    user_id: str
    nick: str
    server_ip: str
    x: float
    y: float
    z: float
    last_seen: float = 0.0
    inflight: Set[str] = field(default_factory=set)
    can_ignite: bool = False    # роль пожарного (поджог/тушение)
    is_admin: bool = False      # роль админа пожара (настройки/действия)


@dataclass
class Incident:
    id: str
    server_ip: str
    created_ts: float
    x: float
    y: float
    z: float


class FireCoordinator:
    def __init__(self, config: Optional[FireConfig] = None, *, clock=None, monotonic=None):
        self.cfg = config or FireConfig()
        import time as _t
        self._clock = clock or _t.time
        self._mono = monotonic or _t.monotonic

        self.cells: Dict[Tuple[str, str], Cell] = {}
        self.incidents: Dict[str, Incident] = {}
        self.workers: Dict[str, Worker] = {}
        self.jobs: Dict[str, Job] = {}
        self._seq = 0

    # ── id helpers (без random/Date — детерминированный счётчик для resume/тестов) ──
    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq}"

    def _grid_key(self, x: float, y: float, z: float) -> str:
        return "%d:%d:%d" % (math.floor(x / self.cfg.grid),
                             math.floor(y / self.cfg.grid),
                             math.floor(z / self.cfg.grid_z))

    # ── Воркеры ──────────────────────────────────────────────────────────────
    def worker_connect(self, user_id: str, nick: str, server_ip: str, pos: dict,
                       can_ignite: bool = False, is_admin: bool = False) -> None:
        self.workers[user_id] = Worker(
            user_id=user_id, nick=nick or "", server_ip=server_ip,
            x=float(pos.get("x", 0)), y=float(pos.get("y", 0)), z=float(pos.get("z", 0)),
            last_seen=self._mono(), can_ignite=can_ignite, is_admin=is_admin,
        )
        logger.info("worker connect user=%s server=%s ignite=%s admin=%s",
                    user_id, server_ip, can_ignite, is_admin)

    def worker_pos(self, user_id: str, server_ip: str, pos: dict, nick: str = "") -> None:
        w = self.workers.get(user_id)
        if not w:
            self.worker_connect(user_id, nick, server_ip, pos)
            return
        w.x = float(pos.get("x", w.x)); w.y = float(pos.get("y", w.y)); w.z = float(pos.get("z", w.z))
        w.server_ip = server_ip or w.server_ip
        w.last_seen = self._mono()

    def worker_disconnect(self, user_id: str) -> None:
        w = self.workers.pop(user_id, None)
        if not w:
            return
        # Реквью заданий ушедшего воркера → снова pending (раздадутся другим).
        for jid in list(w.inflight):
            job = self.jobs.get(jid)
            if job:
                job.assigned_to = None
                job.dispatched_m = 0.0
        logger.info("worker disconnect user=%s (реквью %d заданий)", user_id, len(w.inflight))

    def _online(self, w: Worker, now_m: float) -> bool:
        return (now_m - w.last_seen) <= self.cfg.worker_stale_s

    # ── Инциденты ────────────────────────────────────────────────────────────
    def _pick_incident(self, server_ip: str, x: float, y: float, z: float) -> Incident:
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
        inc = Incident(id=self._next_id("inc"), server_ip=server_ip,
                       created_ts=self._clock(), x=x, y=y, z=z)
        self.incidents[inc.id] = inc
        return inc

    def _incident_cells(self, incident_id: str) -> int:
        return sum(1 for c in self.cells.values()
                   if c.incident_id == incident_id and c.state != STATE_OUT)

    # ── Создание ячейки + place-задания ──────────────────────────────────────
    def _new_place_cell(self, server_ip, gk, x, y, z, nx, ny, nz, incident_id, prefer=None):
        now = self._clock()
        cell = Cell(id=self._next_id("c"), incident_id=incident_id, server_ip=server_ip,
                    grid_key=gk, x=x, y=y, z=z, nx=nx, ny=ny, nz=nz,
                    state=STATE_PROPOSED, created_ts=now)
        self.cells[(server_ip, gk)] = cell
        job = Job(id=self._next_id("j"), kind=KIND_PLACE, server_ip=server_ip, grid_key=gk,
                  x=x, y=y, z=z, nx=nx, ny=ny, nz=nz, incident_id=incident_id, prefer=prefer)
        self.jobs[job.id] = job
        return cell, job

    def ignite(self, user_id: str, x, y, z, nx, ny, nz, server_ip: str) -> dict:
        gk = self._grid_key(x, y, z)
        existing = self.cells.get((server_ip, gk))
        if existing and existing.state != STATE_OUT:
            return {"ok": False, "error": "ALREADY_BURNING",
                    "incidentId": existing.incident_id, "gridKey": gk}
        inc = self._pick_incident(server_ip, x, y, z)
        # Поджигатель предпочтителен как исполнитель своего очага (он рядом).
        self._new_place_cell(server_ip, gk, x, y, z, nx, ny, nz, inc.id, prefer=user_id)
        logger.info("ignite user=%s incident=%s gk=%s @ %.1f,%.1f,%.1f", user_id, inc.id, gk, x, y, z)
        return {"ok": True, "incidentId": inc.id, "gridKey": gk}

    def propose(self, user_id: str, server_ip: str, candidates: list) -> int:
        """Кандидаты распространения от клиента → ячейки proposed + place-задания.
        Дедуп по gridKey (первый побеждает), cooldown и кап incident. Возвращает
        число принятых кандидатов."""
        now = self._clock()
        accepted = 0
        for cand in candidates or []:
            try:
                x, y, z = float(cand["x"]), float(cand["y"]), float(cand["z"])
            except (KeyError, TypeError, ValueError):
                continue
            gk = self._grid_key(x, y, z)
            key = (server_ip, gk)
            ex = self.cells.get(key)
            if ex and ex.state != STATE_OUT:
                continue
            if ex and ex.state == STATE_OUT and (now - ex.out_ts) < self.cfg.cooldown_s:
                continue
            inc_id = str(cand.get("incidentId") or "")
            if not inc_id:
                inc_id = self._pick_incident(server_ip, x, y, z).id
            if self._incident_cells(inc_id) >= self.cfg.max_cells:
                continue
            self._new_place_cell(server_ip, gk, x, y, z,
                                 float(cand.get("nx", 0)), float(cand.get("ny", 0)),
                                 float(cand.get("nz", 1)), inc_id)
            accepted += 1
        return accepted

    # ── Подтверждение заданий ────────────────────────────────────────────────
    def job_done(self, user_id: str, job_id: str, ok: bool, server_id: Optional[int] = None) -> None:
        job = self.jobs.get(job_id)
        w = self.workers.get(user_id)
        if w:
            w.inflight.discard(job_id)
        if not job:
            return
        now = self._clock()
        cell = self.cells.get((job.server_ip, job.grid_key))
        if job.kind == KIND_PLACE:
            if cell and cell.state == STATE_PROPOSED:
                if ok and server_id is not None:
                    cell.state = STATE_BURNING
                    cell.server_object_id = int(server_id)
                    cell.placed_by = user_id
                    cell.burning_ts = now
                    logger.info("burning gk=%s serverId=%s by=%s", cell.grid_key, server_id, user_id)
                else:
                    cell.state = STATE_OUT
                    cell.out_ts = now
                    logger.info("place-failed gk=%s by=%s", cell.grid_key, user_id)
        elif job.kind == KIND_REMOVE:
            if cell and cell.state == STATE_EXTINGUISHING:
                cell.state = STATE_OUT
                cell.out_ts = now
                cell.server_object_id = None
                logger.info("out gk=%s by=%s", cell.grid_key, user_id)
        self.jobs.pop(job_id, None)

    # ── Вода ─────────────────────────────────────────────────────────────────
    def apply_water(self, server_ip: str, items: list) -> None:
        for w in items or []:
            cid = str(w.get("cellId"))
            cell = self._cell_by_id(server_ip, cid)
            if cell and cell.state == STATE_BURNING:
                cell.heat -= float(w.get("amount", 0.0)) * self.cfg.water_factor
                if cell.heat <= 0.0:
                    cell.heat = 0.0
                    cell.state = STATE_EXTINGUISHING
                    self._ensure_remove_job(cell)
                    logger.info("extinguishing gk=%s", cell.grid_key)

    def _ensure_remove_job(self, cell: Cell) -> None:
        if cell.server_object_id is None:
            return
        # уже есть remove-задание для этой ячейки?
        for j in self.jobs.values():
            if j.kind == KIND_REMOVE and j.server_ip == cell.server_ip and j.grid_key == cell.grid_key:
                return
        job = Job(id=self._next_id("j"), kind=KIND_REMOVE, server_ip=cell.server_ip,
                  grid_key=cell.grid_key, x=cell.x, y=cell.y, z=cell.z,
                  incident_id=cell.incident_id, server_object_id=cell.server_object_id,
                  prefer=cell.placed_by)
        self.jobs[job.id] = job

    # ── Тик жизненного цикла ─────────────────────────────────────────────────
    def tick(self) -> None:
        now, now_m = self._clock(), self._mono()
        # офлайн-воркеры → реквью их заданий
        for uid in [u for u, w in self.workers.items() if not self._online(w, now_m)]:
            self.worker_disconnect(uid)
        # heat ramp + extinguishing remove-jobs
        dead = []
        for key, c in self.cells.items():
            if c.state == STATE_BURNING:
                if c.heat < self.cfg.heat_max:
                    c.heat = min(self.cfg.heat_max, c.heat + self.cfg.heat_ramp_per_s)
                # Автозатухание очага (если включено админом): после burn_seconds горения.
                if self.cfg.burn_seconds > 0 and c.burning_ts > 0 \
                        and (now - c.burning_ts) >= self.cfg.burn_seconds:
                    c.state = STATE_EXTINGUISHING
                    c.heat = 0.0
                    self._ensure_remove_job(c)
                    logger.info("auto-burnout gk=%s (горел %.0fс)", c.grid_key, now - c.burning_ts)
            elif c.state == STATE_EXTINGUISHING:
                self._ensure_remove_job(c)
            elif c.state == STATE_OUT and (now - c.out_ts) > self.cfg.cooldown_s:
                dead.append(key)
        for key in dead:
            self.cells.pop(key, None)
        # таймаут заданий → реквью
        for job in self.jobs.values():
            if job.assigned_to and (now_m - job.dispatched_m) > self.cfg.job_timeout_s:
                w = self.workers.get(job.assigned_to)
                if w:
                    w.inflight.discard(job.id)
                job.assigned_to = None
                job.dispatched_m = 0.0
        self._gc_incidents()

    def _gc_incidents(self) -> None:
        alive = {c.incident_id for c in self.cells.values() if c.state != STATE_OUT}
        for iid in [i for i in self.incidents if i not in alive]:
            self.incidents.pop(iid, None)

    # ── Раздача заданий (балансировка) ───────────────────────────────────────
    def dispatch(self) -> List[Tuple[str, dict]]:
        """Назначить pending-задания воркерам. Возвращает [(user_id, job_payload)] для
        отправки по сокетам. Балансировка: наименее загруженный in-range воркер."""
        now_m = self._mono()
        out: List[Tuple[str, dict]] = []
        for job in self.jobs.values():
            if job.assigned_to:
                continue
            w = self._pick_worker(job, now_m)
            if not w:
                continue
            job.assigned_to = w.user_id
            job.dispatched_m = now_m
            w.inflight.add(job.id)
            out.append((w.user_id, job.payload()))
            logger.info("dispatch %s job=%s gk=%s → %s (load=%d)",
                        job.kind, job.id, job.grid_key, w.user_id, len(w.inflight))
        return out

    def _pick_worker(self, job: Job, now_m: float) -> Optional[Worker]:
        pool = [w for w in self.workers.values()
                if w.server_ip == job.server_ip and self._online(w, now_m)]
        # remove own: placer может удалить по id без близости — отдаём ему вне очереди
        if job.kind == KIND_REMOVE and job.prefer:
            pw = self.workers.get(job.prefer)
            if pw and pw in pool and len(pw.inflight) < self.cfg.max_inflight:
                return pw
        pr2 = self.cfg.place_range ** 2
        in_range = []
        for w in pool:
            if len(w.inflight) >= self.cfg.max_inflight:
                continue
            d2 = (w.x - job.x) ** 2 + (w.y - job.y) ** 2 + (w.z - job.z) ** 2
            if d2 <= pr2:
                in_range.append((len(w.inflight), d2, w))
        if not in_range:
            return None
        in_range.sort(key=lambda t: (t[0], t[1]))   # наименьшая загрузка, затем ближе
        return in_range[0][2]

    # ── Состояние для клиента ────────────────────────────────────────────────
    def _cell_by_id(self, server_ip: str, cell_id: str) -> Optional[Cell]:
        for c in self.cells.values():
            if c.server_ip == server_ip and c.id == cell_id:
                return c
        return None

    def state_for(self, user_id: str, near_radius: float = 80.0) -> dict:
        w = self.workers.get(user_id)
        if not w:
            return {"cells_near": [], "caps": self.caps()}
        r2 = near_radius ** 2
        near = []
        for c in self.cells.values():
            if c.server_ip != w.server_ip or c.state == STATE_OUT:
                continue
            d2 = (c.x - w.x) ** 2 + (c.y - w.y) ** 2 + (c.z - w.z) ** 2
            if d2 <= r2:
                near.append(c.to_near())
        return {"cells_near": near, "caps": self.caps()}

    def caps(self) -> dict:
        c = self.cfg
        return {
            "maxCells": c.max_cells, "grid": c.grid, "gridZ": c.grid_z,
            "spreadMinHeat": c.spread_min_heat, "heatMax": c.heat_max,
            "heatRamp": c.heat_ramp_per_s, "placeRange": c.place_range,
            # настраиваемые (клиент читает для спреда/тушения):
            "spreadInterval": c.spread_interval, "spreadChance": c.spread_chance,
            "spreadMaxPerTick": c.spread_max_per_tick,
            "windX": c.wind_x, "windY": c.wind_y, "windStrength": c.wind_strength,
            "slopeBias": c.slope_bias, "fuelBias": c.fuel_bias, "fuelSurfaces": c.fuel_surfaces,
            "burnSeconds": c.burn_seconds,
            "extWaterPerSec": c.ext_water_per_sec, "extRange": c.ext_range, "extRadius": c.ext_radius,
            "paused": c.paused,
        }

    # ── Live-конфиг от админа (меню) ─────────────────────────────────────────
    # Белый список изменяемых полей FireConfig. Меню шлёт {key: value}; применяем и
    # рассылаем новые caps всем клиентам. Числа клампим в разумные пределы.
    _CONFIG_BOUNDS = {
        "spread_interval": (0.3, 30.0), "spread_chance": (0.0, 1.0),
        "spread_max_per_tick": (1, 64), "heat_ramp_per_s": (1.0, 200.0),
        "spread_min_heat": (0.0, 100.0), "burn_seconds": (0.0, 3600.0),
        "wind_x": (-1.0, 1.0), "wind_y": (-1.0, 1.0), "wind_strength": (0.0, 1.0),
        "slope_bias": (0.0, 4.0), "fuel_bias": (0.0, 4.0),
        "ext_water_per_sec": (1.0, 1000.0), "ext_range": (1.0, 60.0), "ext_radius": (0.5, 30.0),
        "max_cells": (1, 1000), "max_inflight": (1, 32), "cooldown_s": (0.0, 600.0),
    }

    def set_config(self, params: dict) -> dict:
        """Применить настройки от админа. Возвращает обновлённый caps."""
        applied = {}
        for k, v in (params or {}).items():
            if k == "fuel_surfaces" and isinstance(v, list):
                self.cfg.fuel_surfaces = [int(s) for s in v if isinstance(s, (int, float))]
                applied[k] = self.cfg.fuel_surfaces
                continue
            b = self._CONFIG_BOUNDS.get(k)
            if not b:
                continue
            try:
                val = float(v)
            except (TypeError, ValueError):
                continue
            val = max(b[0], min(b[1], val))
            if k in ("spread_max_per_tick", "max_cells", "max_inflight"):
                val = int(val)
            setattr(self.cfg, k, val)
            applied[k] = val
        logger.info("fire config set: %s", applied)
        return self.caps()

    # ── Админ ────────────────────────────────────────────────────────────────
    def snapshot(self, server_ip: Optional[str] = None) -> dict:
        cells = [c for c in self.cells.values() if server_ip is None or c.server_ip == server_ip]
        by_state: Dict[str, int] = {}
        for c in cells:
            by_state[c.state] = by_state.get(c.state, 0) + 1
        return {
            "cells": len(cells), "by_state": by_state,
            "incidents": len([i for i in self.incidents.values()
                              if server_ip is None or i.server_ip == server_ip]),
            "workers": len([w for w in self.workers.values()
                            if server_ip is None or w.server_ip == server_ip]),
            "jobs": len([j for j in self.jobs.values()
                         if server_ip is None or j.server_ip == server_ip]),
        }

    def wipe(self, server_ip: Optional[str] = None) -> int:
        """Жёсткий сброс: убрать ячейки и задания (cells_near пуст, спред стоп)."""
        ckeys = [k for k, c in self.cells.items() if server_ip is None or c.server_ip == server_ip]
        for k in ckeys:
            self.cells.pop(k, None)
        jids = [j for j, job in self.jobs.items() if server_ip is None or job.server_ip == server_ip]
        for j in jids:
            self.jobs.pop(j, None)
        for w in self.workers.values():
            if server_ip is None or w.server_ip == server_ip:
                w.inflight.clear()
        self._gc_incidents()
        logger.info("fire wipe: снято %d ячеек, %d заданий (server_ip=%s)", len(ckeys), len(jids), server_ip)
        return len(ckeys)
