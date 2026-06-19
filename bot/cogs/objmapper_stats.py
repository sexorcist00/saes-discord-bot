"""
Cog со статистикой использования скрипта ObjMapper.

Единая интерактивная менюшка /objstats: один эфемерный месседж с Select-меню для
переключения разделов (Обзор / Пользователи / Топы), пагинацией списка пользователей
и выбором конкретного пользователя для детальной статистики.

Доступ — только администраторам главного сервера SAES (декоратор
@commands.has_permissions; команды синкаются только на главный сервер через on_ready).
Данные наполняет HTTP-эндпоинт /api/objmapper/telemetry (батч-хартбит от скрипта).
"""

from datetime import datetime, timezone
from typing import Optional, List

import discord
from discord import app_commands
from discord.ext import commands

from bot.ui.embeds import create_info_embed, create_error_embed, COLOR_PRIMARY
from bot.utils.logger import get_logger

logger = get_logger("cogs.objmapper_stats")

USERS_PER_PAGE = 10


def _rel(value) -> str:
    """Значение TIMESTAMP (UTC) → относительная метка Discord (<t:..:R>) или «—»."""
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value).replace("T", " "))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return f"<t:{int(dt.timestamp())}:R>"
    except (ValueError, TypeError):
        return "—"


def _dur(seconds) -> str:
    """Секунды → «Xч Yм» (или «Yм» / «<1м»)."""
    try:
        s = int(seconds or 0)
    except (TypeError, ValueError):
        return "—"
    if s < 60:
        return "<1м"
    h, m = s // 3600, (s % 3600) // 60
    return f"{h}ч {m}м" if h else f"{m}м"


# ════════════════════════════ интерактивное меню ════════════════════════════

SECTION_OPTIONS = [
    ("📊 Обзор", "overview", "Активность, объёмы, версии, серверы"),
    ("👥 Пользователи", "users", "Список всех: ник + Discord"),
    ("🏆 Топ: объекты", "top:objects", "По числу установленных объектов"),
    ("🏆 Топ: сессии", "top:sessions", "По числу сессий"),
    ("🏆 Топ: время", "top:time", "По времени в скрипте"),
    ("🏆 Топ: модели", "top:models", "Самые используемые модели"),
]


class _SectionSelect(discord.ui.Select):
    def __init__(self, current: str):
        options = [
            discord.SelectOption(
                label=label, value=value, description=desc,
                default=(value == current),
            )
            for label, value, desc in SECTION_OPTIONS
        ]
        super().__init__(placeholder="Выберите раздел…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.view.section = self.values[0]
        self.view.page = 0
        await self.view.refresh(interaction)


class _UserSelect(discord.ui.Select):
    def __init__(self, users: List[dict]):
        options = []
        for u in users:
            nick = (u.get("samp_nick") or "—")[:100]
            did = str(u.get("discord_user_id") or "")
            obj = (u.get("ghost_total", 0) or 0) + (u.get("server_total", 0) or 0)
            options.append(discord.SelectOption(
                label=nick, value=did, description=f"объектов: {obj}"
            ))
        super().__init__(
            placeholder="Открыть пользователя…",
            options=options or [discord.SelectOption(label="—", value="none")],
            row=2,
            disabled=not options,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.section = f"user:{self.values[0]}"
        await self.view.refresh(interaction)


class StatsMenuView(discord.ui.View):
    """Единое меню статистики ObjMapper."""

    def __init__(self, cog, author_id: int, days: int = 30, section: str = "overview"):
        super().__init__(timeout=300.0)
        self.cog = cog
        self.db = cog.bot.db
        self.author_id = author_id
        self.days = days
        self.section = section
        self.page = 0
        self.users: List[dict] = []
        self.message: Optional[discord.Message] = None

        self.section_select = _SectionSelect(self._base_section())
        self.refresh_btn = discord.ui.Button(label="🔄 Обновить", style=discord.ButtonStyle.primary, row=1)
        self.prev_btn = discord.ui.Button(label="◀️", style=discord.ButtonStyle.secondary, row=1)
        self.next_btn = discord.ui.Button(label="▶️", style=discord.ButtonStyle.secondary, row=1)
        self.refresh_btn.callback = self._on_refresh
        self.prev_btn.callback = self._on_prev
        self.next_btn.callback = self._on_next
        self.user_select: Optional[_UserSelect] = None

        self.add_item(self.section_select)
        self.add_item(self.refresh_btn)

    # ── доступ только инициатору (месседж и так эфемерный) ──
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Это меню не для вас.", ephemeral=True)
            return False
        return True

    def _base_section(self) -> str:
        """Какой пункт Select подсветить (детальный просмотр юзера → пункт «Пользователи»)."""
        return "users" if self.section.startswith("user:") else self.section

    async def _on_refresh(self, interaction: discord.Interaction):
        # Сброс кэша списка пользователей → перезагрузится в _ensure_data;
        # обзор/топы/карточка читают БД при каждом построении, поэтому обновятся сами.
        self.users = []
        await self.refresh(interaction)

    async def _on_prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        await self.refresh(interaction)

    async def _on_next(self, interaction: discord.Interaction):
        self.page += 1
        await self.refresh(interaction)

    def _total_pages(self) -> int:
        return max(1, (len(self.users) + USERS_PER_PAGE - 1) // USERS_PER_PAGE)

    def _rebuild_components(self):
        """Пересобрать набор компонентов под текущий раздел."""
        # пересоздаём Select разделов, чтобы корректно подсветить текущий пункт
        self.remove_item(self.section_select)
        self.section_select = _SectionSelect(self._base_section())
        self.add_item(self.section_select)

        is_users = self.section == "users"
        # пагинация — только в списке пользователей
        for btn in (self.prev_btn, self.next_btn):
            if is_users and btn not in self.children:
                self.add_item(btn)
            elif not is_users and btn in self.children:
                self.remove_item(btn)
        if is_users:
            pages = self._total_pages()
            self.page = max(0, min(self.page, pages - 1))
            self.prev_btn.disabled = self.page == 0
            self.next_btn.disabled = self.page >= pages - 1

        # выбор пользователя — только в списке
        if self.user_select and self.user_select in self.children:
            self.remove_item(self.user_select)
            self.user_select = None
        if is_users and self.users:
            chunk = self.users[self.page * USERS_PER_PAGE:(self.page + 1) * USERS_PER_PAGE]
            self.user_select = _UserSelect(chunk)
            self.add_item(self.user_select)

    async def build_embed(self) -> discord.Embed:
        if self.section == "overview":
            return await self.cog.build_overview(self.days)
        if self.section == "users":
            if not self.users:
                self.users = await self.db.get_objmapper_all_users()
            return self._build_users_embed()
        if self.section.startswith("top:"):
            return await self.cog.build_top(self.section.split(":", 1)[1])
        if self.section.startswith("user:"):
            return await self.cog.build_user_by_id(self.section.split(":", 1)[1])
        return await self.cog.build_overview(self.days)

    def _build_users_embed(self) -> discord.Embed:
        total = len(self.users)
        if total == 0:
            return create_info_embed("Пользователей пока нет.", "ObjMapper — пользователи")
        pages = self._total_pages()
        self.page = max(0, min(self.page, pages - 1))
        start = self.page * USERS_PER_PAGE
        chunk = self.users[start:start + USERS_PER_PAGE]
        lines = []
        for i, u in enumerate(chunk, start + 1):
            nick = u.get("samp_nick") or "—"
            did = u.get("discord_user_id")
            mention = f"<@{did}> (`{did}`)" if did else "—"
            obj = (u.get("ghost_total", 0) or 0) + (u.get("server_total", 0) or 0)
            lines.append(f"**{i}. {nick}** — {mention}\n └ был {_rel(u.get('last_seen_at'))} · {obj} об. · {u.get('sessions_total', 0) or 0} сес.")
        embed = discord.Embed(
            title=f"👥 Пользователи ObjMapper — {total}",
            description="\n".join(lines),
            color=COLOR_PRIMARY,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Стр. {self.page + 1}/{pages} · выбери ник ниже для детальной статистики")
        return embed

    async def _ensure_data(self):
        """Подгрузить список пользователей до сборки компонентов (нужен dropdown/пагинация)."""
        if self.section == "users" and not self.users:
            self.users = await self.db.get_objmapper_all_users()

    async def render_initial(self):
        await self._ensure_data()
        self._rebuild_components()
        return await self.build_embed()

    async def refresh(self, interaction: discord.Interaction):
        try:
            await self._ensure_data()
            self._rebuild_components()
            embed = await self.build_embed()
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:  # noqa: BLE001
            logger.error(f"objstats menu refresh error: {e}", exc_info=True)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    embed=create_error_embed(f"Ошибка: {e}"), ephemeral=True
                )

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass
        self.stop()


# ════════════════════════════════ cog ════════════════════════════════

class ObjMapperStatsCog(commands.Cog):
    """Статистика использования ObjMapper для администраторов."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        logger.info("ObjMapperStatsCog загружен")

    @commands.hybrid_command(
        name="objstats",
        description="Меню статистики использования скрипта ObjMapper",
    )
    @commands.has_permissions(administrator=True)
    @app_commands.describe(
        days="Период для обзора, дней (по умолчанию 30)",
        user="Сразу открыть статистику Discord-пользователя",
        nick="Сразу открыть статистику по SA-MP нику",
    )
    async def objstats(
        self,
        ctx: commands.Context,
        days: int = 30,
        user: Optional[discord.User] = None,
        nick: Optional[str] = None,
    ):
        await ctx.defer(ephemeral=True)
        try:
            days = max(1, min(int(days), 365))
            view = StatsMenuView(self, ctx.author.id, days=days)

            # Прямой переход к конкретному пользователю (если задан параметр)
            if user or nick:
                if nick:
                    st = await self.bot.db.get_objmapper_user_stats_by_nick(nick.strip())
                else:
                    st = await self.bot.db.get_objmapper_user_stats(str(user.id))
                if st:
                    view.section = f"user:{st['discord_user_id']}"

            embed = await view.render_initial()
            msg = await ctx.send(embed=embed, view=view, ephemeral=True)
            view.message = msg
            logger.info(f"objstats menu открыт пользователем {ctx.author}")

        except Exception as e:
            logger.error(f"objstats error: {e}", exc_info=True)
            await ctx.send(embed=create_error_embed(f"Ошибка: {e}"), ephemeral=True)

    # ──────────────────────────── overview ────────────────────────────
    async def build_overview(self, days: int) -> discord.Embed:
        db = self.bot.db
        days = max(1, min(int(days), 365))

        active = await db.get_objmapper_active_counts()
        totals = await db.get_objmapper_totals()
        nr = await db.get_objmapper_new_returning(7)
        period = await db.get_objmapper_period_counts(days)
        versions = await db.get_objmapper_version_distribution()
        servers = await db.get_objmapper_server_distribution(limit=5)
        hourly = await db.get_objmapper_hourly()

        if not totals or not totals.get("total_users"):
            return create_info_embed("Данных телеметрии пока нет.", "ObjMapper — обзор")

        embed = discord.Embed(
            title="📊 ObjMapper — обзор использования",
            color=COLOR_PRIMARY,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="👥 Пользователи",
            value=(
                f"Всего: **{totals.get('total_users', 0)}**\n"
                f"Активны сегодня: **{active.get('dau', 0)}**\n"
                f"За 7 дней: **{active.get('wau', 0)}**\n"
                f"За 30 дней: **{active.get('mau', 0)}**"
            ),
            inline=True,
        )
        embed.add_field(
            name="🔄 Удержание (7д)",
            value=(
                f"Новых: **{nr.get('new', 0)}**\n"
                f"Вернувшихся: **{nr.get('returning', 0)}**"
            ),
            inline=True,
        )
        embed.add_field(
            name=f"📦 Объекты за {days}д",
            value=(
                f"Ghost: **{period.get('ghost', 0)}**\n"
                f"Серверных: **{period.get('server', 0)}**\n"
                f"Удалений: **{period.get('deletes', 0)}**\n"
                f"Сессий: **{period.get('sessions', 0)}** · {_dur(period.get('session_seconds'))}"
            ),
            inline=True,
        )
        embed.add_field(
            name="∑ Всего (lifetime)",
            value=(
                f"Ghost: **{totals.get('ghost', 0)}** · "
                f"Серверных: **{totals.get('server', 0)}** · "
                f"Удалений: **{totals.get('deletes', 0)}**\n"
                f"Сессий: **{totals.get('sessions', 0)}** · "
                f"Время: **{_dur(totals.get('session_seconds'))}** · "
                f"Ошибок: **{totals.get('errors', 0)}**"
            ),
            inline=False,
        )
        if versions:
            vlines = [f"`{v['version']}` — {v['count']}" for v in versions[:6]]
            embed.add_field(name="🏷 Версии скрипта", value="\n".join(vlines), inline=True)
        if servers:
            slines = [f"{s['server']} — {s['count']}" for s in servers]
            embed.add_field(name="🌐 Серверы", value="\n".join(slines), inline=True)
        if hourly and any(hourly):
            top_hours = sorted(range(24), key=lambda h: hourly[h], reverse=True)[:3]
            top_hours = [h for h in top_hours if hourly[h] > 0]
            if top_hours:
                hl = " · ".join(f"{h:02d}:00 ({hourly[h]})" for h in top_hours)
                embed.add_field(name="⏰ Пиковые часы (UTC)", value=hl, inline=False)

        embed.set_footer(text=f"Период действий: {days} дн.")
        return embed

    # ───────────────────────────── user ─────────────────────────────
    def _render_user_embed(
        self, stats: Optional[dict], avatar_user, top_models: Optional[list] = None
    ) -> discord.Embed:
        if not stats:
            return create_info_embed("Нет данных телеметрии для этого пользователя.", "ObjMapper — пользователь")

        embed = discord.Embed(
            title="📊 ObjMapper — пользователь",
            color=COLOR_PRIMARY,
            timestamp=datetime.now(timezone.utc),
        )
        author_name = stats.get("samp_nick") or (avatar_user.display_name if avatar_user else "—")
        if avatar_user:
            embed.set_author(name=author_name, icon_url=avatar_user.display_avatar.url)
        else:
            embed.set_author(name=author_name)

        did = stats.get("discord_user_id")
        embed.add_field(
            name="🪪 Discord",
            value=(f"<@{did}> (`{did}`)" if did else "—"),
            inline=False,
        )
        embed.add_field(
            name="🕒 Последние действия",
            value=(
                f"Запуск: {_rel(stats.get('last_launch_at'))}\n"
                f"Меню: {_rel(stats.get('last_menu_at'))}\n"
                f"Ghost: {_rel(stats.get('last_ghost_at'))}\n"
                f"Серверный объект: {_rel(stats.get('last_server_at'))}\n"
                f"Удаление: {_rel(stats.get('last_delete_at'))}\n"
                f"Был онлайн: {_rel(stats.get('last_seen_at'))}"
            ),
            inline=False,
        )
        embed.add_field(
            name="📦 Объёмы (lifetime)",
            value=(
                f"Ghost: **{stats.get('ghost_total', 0)}**\n"
                f"Серверных: **{stats.get('server_total', 0)}**\n"
                f"Удалений: **{stats.get('delete_total', 0)}**\n"
                f"Меню открыто: **{stats.get('menu_total', 0)}**"
            ),
            inline=True,
        )
        sess = stats.get("sessions_total", 0) or 0
        secs = stats.get("session_seconds_total", 0) or 0
        avg = _dur(secs // sess) if sess else "—"
        embed.add_field(
            name="⏱ Сессии",
            value=(
                f"Всего: **{sess}**\n"
                f"Суммарно: **{_dur(secs)}**\n"
                f"В среднем: **{avg}**\n"
                f"Ошибок: **{stats.get('errors_total', 0)}**"
            ),
            inline=True,
        )
        embed.add_field(
            name="🛠 Инструменты",
            value=(
                f"Queue: **{stats.get('tool_queue_total', 0)}** · "
                f"Лента: **{stats.get('tool_tape_total', 0)}** · "
                f"Пресеты: **{stats.get('tool_presets_total', 0)}**"
            ),
            inline=False,
        )
        if top_models:
            lines = []
            for i, m in enumerate(top_models, 1):
                placed = m.get("placed", 0) or 0
                g = m.get("ghost_count", 0) or 0
                s = m.get("server_count", 0) or 0
                d = m.get("delete_count", 0) or 0
                lines.append(
                    f"**{i}.** `{m.get('model_id')}` — поставил **{placed}** "
                    f"(ghost {g} / server {s})" + (f", удалил {d}" if d else "")
                )
            embed.add_field(name="🧱 Топ моделей юзера", value="\n".join(lines), inline=False)

        srv = stats.get("last_server_name") or stats.get("last_server_ip") or "—"
        embed.add_field(
            name="ℹ️ Прочее",
            value=(
                f"Версия: `{stats.get('last_version') or '?'}`\n"
                f"Сервер: {srv}\n"
                f"Первый раз: {_rel(stats.get('first_seen_at'))}"
            ),
            inline=False,
        )
        return embed

    async def build_user_by_id(self, discord_user_id: str) -> discord.Embed:
        stats = await self.bot.db.get_objmapper_user_stats(str(discord_user_id))
        top_models = await self.bot.db.get_objmapper_user_top_models(
            str(discord_user_id), limit=5
        )
        avatar_user = None
        try:
            avatar_user = self.bot.get_user(int(discord_user_id))
        except (TypeError, ValueError):
            avatar_user = None
        return self._render_user_embed(stats, avatar_user, top_models)

    # ───────────────────────────── top ──────────────────────────────
    async def build_top(self, metric: Optional[str]) -> discord.Embed:
        db = self.bot.db
        metric = (metric or "objects").lower()

        embed = discord.Embed(
            title="🏆 ObjMapper — топ",
            color=COLOR_PRIMARY,
            timestamp=datetime.now(timezone.utc),
        )

        if metric == "models":
            rows = await db.get_objmapper_top_models(limit=15)
            if not rows:
                return create_info_embed("Нет данных.", "Топ моделей")
            lines = [f"**{i}.** `{r['model_id']}` — {r['count']}" for i, r in enumerate(rows, 1)]
            embed.add_field(name="Самые используемые модели", value="\n".join(lines), inline=False)
        else:
            rows = await db.get_objmapper_top_users(metric=metric, limit=15)
            if not rows:
                return create_info_embed("Нет данных.", "Топ пользователей")
            lines = []
            for i, r in enumerate(rows, 1):
                name = r.get("samp_nick") or r.get("discord_user_id") or "?"
                if metric == "sessions":
                    val = f"{r.get('sessions_total', 0)} сессий"
                elif metric == "time":
                    val = _dur(r.get("session_seconds_total"))
                else:
                    val = f"{r.get('objects_total', 0)} объектов"
                lines.append(f"**{i}.** {name} — {val}")
            title = {"sessions": "по сессиям", "time": "по времени"}.get(metric, "по объектам")
            embed.add_field(name=f"Топ пользователей ({title})", value="\n".join(lines), inline=False)

        return embed

    @objstats.error
    async def objstats_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(
                embed=create_error_embed(
                    "У вас нет прав для этой команды.", "Недостаточно прав"
                ),
                ephemeral=True,
            )
        else:
            logger.error(f"Ошибка в команде objstats: {error}", exc_info=True)
            await ctx.send(embed=create_error_embed(f"Произошла ошибка: {error}"), ephemeral=True)


async def setup(bot):
    await bot.add_cog(ObjMapperStatsCog(bot))
    logger.info("ObjMapperStatsCog добавлен в бота")
