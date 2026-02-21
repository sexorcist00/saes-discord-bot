"""
Cog с командами просмотра статистики
"""

import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional
from datetime import datetime, timedelta
import io

from bot.ui.embeds import create_stats_embed, create_info_embed, create_error_embed, create_sync_history_page
from bot.ui.buttons import PaginationView
from bot.utils.logger import get_logger

logger = get_logger("cogs.stats_commands")


class StatsCommandsCog(commands.Cog):
    """Cog с командами статистики"""

    def __init__(self, bot):
        """
        Инициализация Cog

        Args:
            bot: Объект бота
        """
        self.bot = bot

    async def cog_load(self):
        """Вызывается когда Cog загружается"""
        logger.info("StatsCommandsCog загружен")

    @commands.hybrid_group(name="rolestats", invoke_without_command=True, description="Команды статистики синхронизации")
    @commands.has_permissions(administrator=True)
    async def role_stats(self, ctx: commands.Context):
        """Группа команд статистики синхронизации"""
        if ctx.invoked_subcommand is None:
            help_text = (
                "**Доступные команды статистики:**\n\n"
                "`/rolestats overview` - Общая статистика\n"
                "`/rolestats user [@пользователь]` - Статистика пользователя\n"
                "`/rolestats history [лимит] [пользователь]` - История синхронизаций\n"
                "`/rolestats logs [лимит]` - Последние логи синхронизации\n"
                "`/rolestats daily [дней]` - Ежедневная статистика\n"
            )
            await ctx.send(embed=create_info_embed(help_text, "Команды статистики"), ephemeral=True)

    @role_stats.command(name="overview", description="Показать общую статистику синхронизации")
    @app_commands.describe(days="Количество дней для статистики (по умолчанию 30)")
    async def stats_overview(self, ctx: commands.Context, days: int = 30):
        """Показать общую статистику синхронизации"""
        try:
            # Получаем статистику из БД
            stats = await self.bot.db.get_statistics_summary(days=days)

            if not stats or all(v == 0 or v is None for v in stats.values()):
                await ctx.send(
                    embed=create_info_embed(
                        f"Нет данных за последние {days} дней.",
                        "Статистика отсутствует"
                    ),
                    ephemeral=True
                )
                return

            # Создаем embed со статистикой
            embed = create_stats_embed(stats)
            embed.set_footer(text=f"Статистика за последние {days} дней")

            await ctx.send(embed=embed, ephemeral=True)

            logger.info(f"Статистика запрошена пользователем {ctx.author}")

        except Exception as e:
            logger.error(f"Ошибка получения статистики: {e}", exc_info=True)
            await ctx.send(embed=create_error_embed(f"Ошибка: {e}"), ephemeral=True)

    @role_stats.command(name="user", description="Показать статистику синхронизации для пользователя")
    @app_commands.describe(user="Пользователь (по умолчанию - вы)")
    async def user_stats(
        self,
        ctx: commands.Context,
        user: Optional[discord.User] = None
    ):
        """Показать статистику синхронизации для пользователя"""
        target_user = user or ctx.author

        try:
            # Получаем историю синхронизаций пользователя
            assignments = await self.bot.db.get_user_role_assignments(
                user_id=target_user.id,
                limit=10
            )

            # Получаем состояние синхронизации
            main_server_id = self.bot.config.get_main_server_id()
            sync_state = await self.bot.db.get_sync_state(
                user_id=target_user.id,
                main_server_id=main_server_id
            )

            # Создаем embed
            embed = discord.Embed(
                title=f"📊 Статистика синхронизации",
                color=0x3498db,
                timestamp=datetime.now()
            )

            embed.set_author(
                name=target_user.display_name,
                icon_url=target_user.display_avatar.url
            )

            if sync_state:
                embed.add_field(
                    name="Последняя синхронизация",
                    value=f"<t:{int(datetime.fromisoformat(sync_state['last_sync_timestamp']).timestamp())}:R>",
                    inline=True
                )
                embed.add_field(
                    name="Всего синхронизаций",
                    value=str(sync_state['sync_count']),
                    inline=True
                )
            else:
                embed.description = "Синхронизаций ещё не было."

            if assignments:
                embed.add_field(
                    name="Последние назначения ролей",
                    value=str(len(assignments)),
                    inline=True
                )

                # Показываем последние 5 назначений
                recent_text = []
                for assignment in assignments[:5]:
                    timestamp = datetime.fromisoformat(assignment['assigned_timestamp'])
                    recent_text.append(
                        f"• <t:{int(timestamp.timestamp())}:R> - "
                        f"Роль `{assignment['target_role_id']}` ({assignment['assignment_type']})"
                    )

                if recent_text:
                    embed.add_field(
                        name="Недавние назначения",
                        value="\n".join(recent_text),
                        inline=False
                    )

            await ctx.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Ошибка получения статистики пользователя: {e}", exc_info=True)
            await ctx.send(embed=create_error_embed(f"Ошибка: {e}"), ephemeral=True)

    @role_stats.command(name="history", description="Показать историю синхронизаций с деталями")
    @app_commands.describe(
        limit="Количество записей (по умолчанию 20, макс 100)",
        user="Фильтр по пользователю (опционально)"
    )
    async def sync_history(
        self,
        ctx: commands.Context,
        limit: int = 20,
        user: Optional[discord.User] = None
    ):
        """Показать историю синхронизаций с ролями, ошибками и статусами"""
        if limit > 100:
            await ctx.send("Максимальный лимит: 100 записей", ephemeral=True)
            limit = 100

        try:
            user_id = user.id if user else None
            sessions = await self.bot.db.get_recent_sync_sessions(limit=limit, user_id=user_id)

            if not sessions:
                title = "Нет данных"
                if user:
                    msg = f"История синхронизаций для {user.mention} отсутствует."
                else:
                    msg = "История синхронизаций отсутствует."
                await ctx.send(embed=create_info_embed(msg, title), ephemeral=True)
                return

            # Разбиваем на страницы по 5 сессий
            page_size = 5
            pages = []
            total_pages = (len(sessions) - 1) // page_size + 1

            for i in range(0, len(sessions), page_size):
                page_sessions = sessions[i:i + page_size]
                page_num = i // page_size + 1
                page_embed = create_sync_history_page(
                    sessions=page_sessions,
                    guild=ctx.guild,
                    page=page_num,
                    total_pages=total_pages
                )
                if user:
                    page_embed.set_author(
                        name=f"Фильтр: {user.display_name}",
                        icon_url=user.display_avatar.url
                    )
                pages.append(page_embed)

            if len(pages) == 1:
                await ctx.send(embed=pages[0], ephemeral=True)
            else:
                view = PaginationView(pages)
                await ctx.send(embed=pages[0], view=view, ephemeral=True)

        except Exception as e:
            logger.error(f"Ошибка получения истории синхронизаций: {e}", exc_info=True)
            await ctx.send(embed=create_error_embed(f"Ошибка: {e}"), ephemeral=True)

    @role_stats.command(name="logs", description="Показать последние логи синхронизации")
    @app_commands.describe(limit="Количество записей для отображения (по умолчанию 20, макс 100)")
    async def recent_logs(self, ctx: commands.Context, limit: int = 20):
        """Показать последние логи синхронизации"""
        if limit > 100:
            await ctx.send("⚠️ Максимальный лимит: 100 записей", ephemeral=True)
            limit = 100

        try:
            # Получаем логи
            logs = await self.bot.db.get_recent_logs(limit=limit)

            if not logs:
                await ctx.send(
                    embed=create_info_embed(
                        "Логи синхронизации отсутствуют.",
                        "Нет данных"
                    ),
                    ephemeral=True
                )
                return

            # Создаем embed с логами
            embed = discord.Embed(
                title=f"📜 Последние {len(logs)} логов синхронизации",
                color=0x3498db,
                timestamp=datetime.now()
            )

            # Группируем логи по страницам (по 10 на странице)
            page_size = 10
            pages = []

            for i in range(0, len(logs), page_size):
                page_logs = logs[i:i+page_size]
                page_embed = discord.Embed(
                    title=f"📜 Логи синхронизации",
                    color=0x3498db,
                    timestamp=datetime.now()
                )

                log_text = []
                for log in page_logs:
                    timestamp = datetime.fromisoformat(log['timestamp'])
                    status_emoji = "✅" if log['success'] else "❌"

                    log_line = (
                        f"{status_emoji} <t:{int(timestamp.timestamp())}:t> - "
                        f"Пользователь `{log['user_id']}` - "
                        f"{log['action_type']} ({log['trigger_type']})"
                    )

                    if log.get('error_message'):
                        log_line += f"\n   └─ Ошибка: {log['error_message'][:50]}"

                    log_text.append(log_line)

                page_embed.description = "\n".join(log_text)
                page_embed.set_footer(
                    text=f"Страница {len(pages) + 1}/{(len(logs) - 1) // page_size + 1}"
                )

                pages.append(page_embed)

            # Если только одна страница
            if len(pages) == 1:
                await ctx.send(embed=pages[0], ephemeral=True)
            else:
                # Используем пагинацию
                view = PaginationView(pages)
                await ctx.send(embed=pages[0], view=view, ephemeral=True)

        except Exception as e:
            logger.error(f"Ошибка получения логов: {e}", exc_info=True)
            await ctx.send(embed=create_error_embed(f"Ошибка: {e}"), ephemeral=True)

    @role_stats.command(name="daily", description="Показать ежедневную статистику")
    @app_commands.describe(days="Количество дней (по умолчанию 7, макс 30)")
    async def daily_stats(self, ctx: commands.Context, days: int = 7):
        """Показать ежедневную статистику"""
        if days > 30:
            await ctx.send("⚠️ Максимальный период: 30 дней", ephemeral=True)
            days = 30

        try:
            # Получаем ежедневную статистику
            daily_stats = await self.bot.db.get_daily_statistics(days=days)

            if not daily_stats:
                await ctx.send(
                    embed=create_info_embed(
                        f"Нет данных за последние {days} дней.",
                        "Статистика отсутствует"
                    ),
                    ephemeral=True
                )
                return

            # Создаем embed
            embed = discord.Embed(
                title=f"📅 Ежедневная статистика ({days} дней)",
                color=0x3498db,
                timestamp=datetime.now()
            )

            # Форматируем данные
            stats_text = []
            for stat in daily_stats:
                date = stat['stat_date']
                total = stat['total_syncs'] or 0
                success = stat['successful_syncs'] or 0
                failed = stat['failed_syncs'] or 0

                success_rate = (success / total * 100) if total > 0 else 0

                stats_text.append(
                    f"**{date}**\n"
                    f"  Синхронизаций: {total} (✅ {success}, ❌ {failed})\n"
                    f"  Успешность: {success_rate:.1f}%"
                )

            embed.description = "\n\n".join(stats_text)

            await ctx.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Ошибка получения ежедневной статистики: {e}", exc_info=True)
            await ctx.send(embed=create_error_embed(f"Ошибка: {e}"), ephemeral=True)

    @role_stats.command(name="export", description="Экспортировать статистику в CSV файл")
    @commands.has_permissions(administrator=True)
    @app_commands.describe(days="Количество дней для экспорта (по умолчанию 30)")
    async def export_stats(self, ctx: commands.Context, days: int = 30):
        """Экспортировать статистику в CSV файл"""
        try:
            # Получаем все логи за период
            logs = await self.bot.db.get_recent_logs(limit=10000)

            if not logs:
                await ctx.send(
                    embed=create_info_embed(
                        "Нет данных для экспорта.",
                        "Экспорт невозможен"
                    ),
                    ephemeral=True
                )
                return

            # Формируем CSV
            csv_lines = [
                "timestamp,user_id,action_type,trigger_type,success,error_message"
            ]

            for log in logs:
                csv_lines.append(
                    f"{log['timestamp']},"
                    f"{log['user_id']},"
                    f"{log['action_type']},"
                    f"{log['trigger_type']},"
                    f"{log['success']},"
                    f"\"{log.get('error_message', '')}\""
                )

            csv_content = "\n".join(csv_lines)

            # Создаем файл
            file = discord.File(
                io.BytesIO(csv_content.encode('utf-8')),
                filename=f"sync_stats_{datetime.now().strftime('%Y%m%d')}.csv"
            )

            await ctx.send(
                embed=create_info_embed(
                    f"Экспорт статистики за {days} дней готов.\n"
                    f"Записей: {len(logs)}",
                    "Экспорт завершен"
                ),
                file=file,
                ephemeral=True
            )

            logger.info(f"Статистика экспортирована пользователем {ctx.author}")

        except Exception as e:
            logger.error(f"Ошибка экспорта статистики: {e}", exc_info=True)
            await ctx.send(embed=create_error_embed(f"Ошибка: {e}"), ephemeral=True)

    @role_stats.error
    async def role_stats_error(self, ctx: commands.Context, error: Exception):
        """Обработчик ошибок группы команд role_stats"""
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(
                embed=create_error_embed(
                    "У вас нет прав для использования этой команды.",
                    "Недостаточно прав"
                ),
                ephemeral=True
            )
        elif isinstance(error, commands.BadArgument):
            await ctx.send(
                embed=create_error_embed(
                    "Некорректный аргумент команды. Проверьте синтаксис.",
                    "Неверный аргумент"
                ),
                ephemeral=True
            )
        else:
            logger.error(f"Ошибка в команде role_stats: {error}", exc_info=True)
            await ctx.send(embed=create_error_embed(f"Произошла ошибка: {error}"), ephemeral=True)


async def setup(bot):
    """
    Функция для загрузки Cog

    Args:
        bot: Объект бота
    """
    await bot.add_cog(StatsCommandsCog(bot))
    logger.info("StatsCommandsCog добавлен в бота")
