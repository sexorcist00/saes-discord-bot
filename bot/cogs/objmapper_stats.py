"""
Cog со статистикой использования скрипта ObjMapper.

Единая команда /objstats с параметром view (overview | user | top). Доступ — только
администраторам главного сервера SAES (декоратор @commands.has_permissions; команды
синкаются только на главный сервер через on_ready). Данные наполняет HTTP-эндпоинт
/api/objmapper/telemetry (батч-хартбит от скрипта).
"""

from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.ui.embeds import create_info_embed, create_error_embed, COLOR_PRIMARY
from bot.utils.logger import get_logger

logger = get_logger("cogs.objmapper_stats")


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


class ObjMapperStatsCog(commands.Cog):
    """Статистика использования ObjMapper для администраторов."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        logger.info("ObjMapperStatsCog загружен")

    @commands.hybrid_command(
        name="objstats",
        description="Статистика использования скрипта ObjMapper",
    )
    @commands.has_permissions(administrator=True)
    @app_commands.describe(
        view="Что показать: обзор / пользователь / топ (по умолчанию обзор)",
        days="Период для сумм и удержания, дней (только для обзора; по умолчанию 30)",
        user="Discord-пользователь (для view=user; по умолчанию — вы)",
        nick="SA-MP ник (для view=user; альтернатива выбору пользователя)",
        metric="Метрика лидерборда (для view=top)",
    )
    @app_commands.choices(
        view=[
            app_commands.Choice(name="Обзор", value="overview"),
            app_commands.Choice(name="Пользователь", value="user"),
            app_commands.Choice(name="Топ", value="top"),
        ],
        metric=[
            app_commands.Choice(name="Объекты (ghost+server)", value="objects"),
            app_commands.Choice(name="Сессии", value="sessions"),
            app_commands.Choice(name="Время в скрипте", value="time"),
            app_commands.Choice(name="Топ моделей", value="models"),
        ],
    )
    async def objstats(
        self,
        ctx: commands.Context,
        view: Optional[str] = "overview",
        days: int = 30,
        user: Optional[discord.User] = None,
        nick: Optional[str] = None,
        metric: Optional[str] = "objects",
    ):
        await ctx.defer(ephemeral=True)
        try:
            view = (view or "overview").lower()
            if view == "user":
                embed = await self._build_user(ctx, user, nick)
            elif view == "top":
                embed = await self._build_top(metric)
            else:
                embed = await self._build_overview(days)

            await ctx.send(embed=embed, ephemeral=True)
            logger.info(f"objstats view={view} запрошен пользователем {ctx.author}")

        except Exception as e:
            logger.error(f"objstats error (view={view}): {e}", exc_info=True)
            await ctx.send(embed=create_error_embed(f"Ошибка: {e}"), ephemeral=True)

    # ──────────────────────────── overview ────────────────────────────
    async def _build_overview(self, days: int) -> discord.Embed:
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
    async def _build_user(
        self, ctx: commands.Context, user: Optional[discord.User], nick: Optional[str]
    ) -> discord.Embed:
        db = self.bot.db
        avatar_user = None
        if nick:
            stats = await db.get_objmapper_user_stats_by_nick(nick.strip())
        else:
            target = user or ctx.author
            avatar_user = target
            stats = await db.get_objmapper_user_stats(str(target.id))

        if not stats:
            who = nick or (user.mention if user else "вы")
            return create_info_embed(
                f"Нет данных телеметрии для {who}.", "ObjMapper — пользователь"
            )

        embed = discord.Embed(
            title="📊 ObjMapper — пользователь",
            color=COLOR_PRIMARY,
            timestamp=datetime.now(timezone.utc),
        )
        if avatar_user is None:
            try:
                avatar_user = self.bot.get_user(int(stats["discord_user_id"]))
            except (TypeError, ValueError):
                avatar_user = None
        author_name = stats.get("samp_nick") or (avatar_user.display_name if avatar_user else "—")
        if avatar_user:
            embed.set_author(name=author_name, icon_url=avatar_user.display_avatar.url)
        else:
            embed.set_author(name=author_name)

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

    # ───────────────────────────── top ──────────────────────────────
    async def _build_top(self, metric: Optional[str]) -> discord.Embed:
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
