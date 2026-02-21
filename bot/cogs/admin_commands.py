"""
Cog с административными командами для управления ботом
"""

import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional
import asyncio
import time

from bot.core.sync_engine import SyncEngine
from bot.core.role_mapper import RoleMapper
from bot.core.permissions import validate_all_servers, format_permissions_report
from bot.ui.embeds import (
    create_success_embed,
    create_error_embed,
    create_info_embed,
    create_mapping_list_embed
)
from bot.config import RoleMapping
from bot.utils.logger import get_logger
from bot.utils.validators import validate_server_id, validate_role_id

logger = get_logger("cogs.admin_commands")


class _ConfirmSyncView(discord.ui.View):
    """View с кнопками подтверждения/отмены массовой синхронизации"""

    def __init__(self, author_id: int):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.confirmed = False

    @discord.ui.button(label="Подтвердить", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Эта кнопка не для вас.", ephemeral=True)
            return
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Эта кнопка не для вас.", ephemeral=True)
            return
        self.confirmed = False
        await interaction.response.defer()
        self.stop()

    async def on_timeout(self):
        self.confirmed = False
        self.stop()


class AdminCommandsCog(commands.Cog):
    """Cog с административными командами"""

    def __init__(self, bot):
        """
        Инициализация Cog

        Args:
            bot: Объект бота
        """
        self.bot = bot
        self.sync_engine: Optional[SyncEngine] = None
        self.role_mapper: Optional[RoleMapper] = None

    async def cog_load(self):
        """Вызывается когда Cog загружается"""
        logger.info("AdminCommandsCog загружен")

        # Создаем RoleMapper и SyncEngine
        self.role_mapper = RoleMapper(self.bot.config, self.bot.db)
        await self.role_mapper.initialize()

        self.sync_engine = SyncEngine(
            bot=self.bot,
            config=self.bot.config,
            db=self.bot.db,
            role_mapper=self.role_mapper
        )

    @commands.hybrid_group(name="roleadmin", invoke_without_command=True, description="Команды администрирования ролей")
    @commands.has_permissions(administrator=True)
    async def role_admin(self, ctx: commands.Context):
        """
        Группа команд администрирования ролей

        Использование: !roleadmin <подкоманда> или /roleadmin <подкоманда>
        """
        if ctx.invoked_subcommand is None:
            help_text = (
                "**Доступные команды администрирования:**\n\n"
                "**Синхронизация:**\n"
                "`/roleadmin sync_user <ID>` - Синхронизировать пользователя\n"
                "`/roleadmin sync_all` - Синхронизировать всех\n"
                "`/roleadmin autosync` - Вкл/выкл автосинхронизации\n\n"
                "**Маппинги:**\n"
                "`/roleadmin list_mappings` - Список маппингов\n"
                "`/roleadmin add_mapping` - Добавить маппинг\n"
                "`/roleadmin remove_mapping <ID>` - Удалить маппинг\n\n"
                "**Система:**\n"
                "`/roleadmin reload_config` - Перезагрузить конфигурацию\n"
                "`/roleadmin check_permissions` - Проверить права бота\n"
                "`/roleadmin debug_user <ID>` - Диагностика пользователя\n"
            )
            await ctx.send(embed=create_info_embed(help_text, "Команды администрирования"), ephemeral=True)

    @role_admin.command(name="sync_all", description="Синхронизировать всех пользователей на главном сервере")
    async def sync_all_users(self, ctx: commands.Context):
        """Синхронизировать всех пользователей на главном сервере"""
        # Проверяем включена ли массовая синхронизация в конфиге
        if not self.bot.config.is_batch_sync_enabled():
            await ctx.send(
                embed=create_error_embed(
                    "Массовая синхронизация отключена в конфигурации.",
                    "Функция недоступна"
                ),
                ephemeral=True
            )
            return

        # Подтверждение
        main_server_id = self.bot.config.get_main_server_id()
        guild = self.bot.get_guild(main_server_id)

        if not guild:
            await ctx.send(embed=create_error_embed("Главный сервер не найден."), ephemeral=True)
            return

        # Считаем количество пользователей
        non_bot_members = [m for m in guild.members if not m.bot]
        member_count = len(non_bot_members)

        # Кнопка подтверждения
        confirm_view = _ConfirmSyncView(ctx.author.id)
        confirm_msg = await ctx.send(
            embed=create_info_embed(
                f"Вы собираетесь синхронизировать **{member_count}** пользователей.\n"
                f"Это может занять некоторое время.",
                "Подтверждение массовой синхронизации"
            ),
            view=confirm_view,
            ephemeral=True
        )

        await confirm_view.wait()

        if not confirm_view.confirmed:
            try:
                await confirm_msg.edit(
                    embed=create_info_embed("Массовая синхронизация отменена.", "Отменено"),
                    view=None
                )
            except Exception:
                pass
            return

        try:
            await confirm_msg.edit(view=None)
        except Exception:
            pass

        # Выполняем массовую синхронизацию
        progress_msg = await ctx.send(
            embed=create_info_embed(
                f"**Прогресс:** 0/{member_count} (0%)\n"
                f"`{'░' * 20}`\n\n"
                f"Предзагрузка участников...",
                "Массовая синхронизация..."
            ),
            ephemeral=True
        )

        try:
            last_update_time = time.monotonic()

            async def progress_callback(processed: int, total: int, stats: dict):
                nonlocal last_update_time
                now = time.monotonic()
                # Обновляем embed не чаще раза в 5 секунд (или в конце)
                if now - last_update_time < 5 and processed < total:
                    return
                last_update_time = now

                percent = int(processed / total * 100) if total > 0 else 0
                bar_filled = percent // 5
                progress_bar = "\u2588" * bar_filled + "\u2591" * (20 - bar_filled)

                progress_embed = create_info_embed(
                    f"**Прогресс:** {processed}/{total} ({percent}%)\n"
                    f"`{progress_bar}`\n\n"
                    f"\u2705 Успешно: {stats.get('success', 0)}\n"
                    f"\u274c Ошибок: {stats.get('failed', 0)}\n"
                    f"\u2796 Без изменений: {stats.get('no_changes', 0)}",
                    "Массовая синхронизация..."
                )
                try:
                    await progress_msg.edit(embed=progress_embed)
                except Exception:
                    pass

            stats = await self.sync_engine.sync_all_users(
                guild_id=main_server_id,
                progress_callback=progress_callback
            )

            # Финальный результат
            result_lines = [
                f"**Результаты массовой синхронизации:**\n",
                f"\u2705 Успешно: {stats.get('success', 0)}",
                f"\u274c Ошибок: {stats.get('failed', 0)}",
                f"\u2796 Без изменений: {stats.get('no_changes', 0)}",
                f"\u23ed\ufe0f Пропущено (боты): {stats.get('skipped', 0)}",
                f"\ud83d\udcca Всего обработано: {stats.get('total', 0)}"
            ]
            if stats.get('db_errors', 0) > 0:
                result_lines.append(
                    f"\n\u26a0\ufe0f Ошибки записи в БД: {stats['db_errors']} "
                    f"(часть данных может быть не сохранена)"
                )
            result_text = "\n".join(result_lines)

            await progress_msg.edit(
                embed=create_success_embed(result_text, "Массовая синхронизация завершена")
            )

            logger.info(
                f"Массовая синхронизация выполнена пользователем {ctx.author}: {stats}"
            )

        except Exception as e:
            logger.error(f"Ошибка массовой синхронизации: {e}", exc_info=True)
            await progress_msg.edit(
                embed=create_error_embed(f"Ошибка при массовой синхронизации: {e}")
            )

    @role_admin.command(name="sync_user", description="Синхронизировать конкретного пользователя")
    @app_commands.describe(user_id="ID пользователя Discord (Discord Snowflake)")
    async def sync_specific_user(self, ctx: commands.Context, user_id: str):
        """Синхронизировать конкретного пользователя"""
        try:
            # Преобразование в int
            try:
                user_id_int = int(user_id)
            except ValueError:
                await ctx.send(
                    embed=create_error_embed("ID пользователя должен быть числом (Discord Snowflake)."),
                    ephemeral=True
                )
                return

            # Выполняем синхронизацию
            result = await self.sync_engine.sync_user_roles(
                user_id=user_id_int,
                trigger_type="manual"
            )

            # Формируем детальный результат
            main_guild = self.bot.get_guild(self.bot.config.get_main_server_id())
            lines = [f"**Синхронизация пользователя <@{user_id_int}>:**\n"]

            if result.roles_added:
                added = []
                for rid in result.roles_added:
                    role = main_guild.get_role(rid) if main_guild else None
                    added.append(role.mention if role else f"`{rid}`")
                lines.append(f"➕ Добавлено: {', '.join(added)}")

            if result.roles_removed:
                removed = []
                for rid in result.roles_removed:
                    role = main_guild.get_role(rid) if main_guild else None
                    removed.append(role.mention if role else f"`{rid}`")
                lines.append(f"➖ Удалено: {', '.join(removed)}")

            if result.roles_failed:
                failed = []
                for rid in result.roles_failed:
                    role = main_guild.get_role(rid) if main_guild else None
                    failed.append(role.mention if role else f"`{rid}`")
                lines.append(f"⚠️ Не удалось выдать: {', '.join(failed)}")

            if not result.roles_added and not result.roles_removed and not result.roles_failed:
                lines.append("Без изменений")

            lines.append(f"\n📊 Проверено серверов: {len(result.source_servers)}")

            if result.success:
                await ctx.send(embed=create_success_embed("\n".join(lines)), ephemeral=True)
            else:
                if result.errors:
                    lines.append(f"\n❌ Ошибки: {'; '.join(result.errors[:3])}")
                await ctx.send(embed=create_error_embed("\n".join(lines), "Синхронизация с ошибками"), ephemeral=True)

            logger.info(f"Ручная синхронизация пользователя {user_id_int} выполнена {ctx.author}")

        except Exception as e:
            logger.error(f"Ошибка синхронизации пользователя {user_id}: {e}", exc_info=True)
            await ctx.send(embed=create_error_embed(f"Ошибка: {e}"), ephemeral=True)

    @role_admin.command(name="list_mappings", description="Показать все маппинги ролей")
    async def list_mappings(self, ctx: commands.Context):
        """Показать все маппинги ролей"""
        try:
            mappings = await self.bot.db.get_all_mappings()

            if not mappings:
                await ctx.send(
                    embed=create_info_embed(
                        "Маппинги ролей не настроены.\n"
                        "Используйте `!roleadmin add_mapping` для добавления.",
                        "Список маппингов пуст"
                    ),
                    ephemeral=True
                )
                return

            # Создаем embed со списком
            embed = create_mapping_list_embed([dict(m) for m in mappings])
            await ctx.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Ошибка получения списка маппингов: {e}", exc_info=True)
            await ctx.send(embed=create_error_embed(f"Ошибка: {e}"), ephemeral=True)

    @role_admin.command(name="add_mapping", description="Добавить новый маппинг роли")
    @app_commands.describe(
        mapping_id="Уникальный ID маппинга",
        source_server="ID исходного сервера (Discord Snowflake)",
        source_role="ID исходной роли (Discord Snowflake)",
        target_role="ID целевой роли (Discord Snowflake)",
        description="Описание маппинга"
    )
    async def add_mapping(
        self,
        ctx: commands.Context,
        mapping_id: str,
        source_server: str,
        source_role: str,
        target_role: str,
        *,
        description: str = ""
    ):
        """Добавить новый маппинг роли"""
        try:
            # Преобразование строк в int
            try:
                source_server_id = int(source_server)
                source_role_id = int(source_role)
                target_role_id = int(target_role)
            except ValueError:
                await ctx.send(
                    embed=create_error_embed("ID должны быть числами (Discord Snowflake)."),
                    ephemeral=True
                )
                return

            # Валидация
            if not validate_server_id(source_server_id):
                await ctx.send(embed=create_error_embed("Некорректный ID исходного сервера."), ephemeral=True)
                return

            if not validate_role_id(source_role_id) or not validate_role_id(target_role_id):
                await ctx.send(embed=create_error_embed("Некорректный ID роли."), ephemeral=True)
                return

            # Добавляем маппинг
            main_server_id = self.bot.config.get_main_server_id()

            await self.role_mapper.add_mapping(
                mapping_id=mapping_id,
                source_server_id=source_server_id,
                source_role_id=source_role_id,
                target_server_id=main_server_id,
                target_role_id=target_role_id,
                description=description,
                enabled=True
            )

            await ctx.send(
                embed=create_success_embed(
                    f"Маппинг `{mapping_id}` успешно добавлен!",
                    "Маппинг добавлен"
                ),
                ephemeral=True
            )

            logger.info(f"Маппинг {mapping_id} добавлен пользователем {ctx.author}")

        except Exception as e:
            logger.error(f"Ошибка добавления маппинга: {e}", exc_info=True)
            await ctx.send(embed=create_error_embed(f"Ошибка: {e}"), ephemeral=True)

    @role_admin.command(name="remove_mapping", description="Удалить маппинг роли")
    @app_commands.describe(mapping_id="ID маппинга для удаления")
    async def remove_mapping(self, ctx: commands.Context, mapping_id: str):
        """Удалить маппинг роли"""
        try:
            success = await self.role_mapper.remove_mapping(mapping_id)

            if success:
                await ctx.send(
                    embed=create_success_embed(
                        f"Маппинг `{mapping_id}` успешно удален!",
                        "Маппинг удален"
                    ),
                    ephemeral=True
                )
                logger.info(f"Маппинг {mapping_id} удален пользователем {ctx.author}")
            else:
                await ctx.send(
                    embed=create_error_embed(
                        f"Маппинг `{mapping_id}` не найден.",
                        "Маппинг не найден"
                    ),
                    ephemeral=True
                )

        except Exception as e:
            logger.error(f"Ошибка удаления маппинга: {e}", exc_info=True)
            await ctx.send(embed=create_error_embed(f"Ошибка: {e}"), ephemeral=True)

    @role_admin.command(name="reload_config", description="Перезагрузить конфигурацию из файлов")
    async def reload_config(self, ctx: commands.Context):
        """Перезагрузить конфигурацию из файлов"""
        try:
            # Перезагружаем маппинги
            await self.role_mapper.reload_mappings()

            await ctx.send(
                embed=create_success_embed(
                    "Конфигурация успешно перезагружена!",
                    "Конфигурация обновлена"
                ),
                ephemeral=True
            )

            logger.info(f"Конфигурация перезагружена пользователем {ctx.author}")

        except Exception as e:
            logger.error(f"Ошибка перезагрузки конфигурации: {e}", exc_info=True)
            await ctx.send(embed=create_error_embed(f"Ошибка: {e}"), ephemeral=True)

    @role_admin.command(name="check_permissions", description="Проверить права бота на всех серверах")
    async def check_permissions(self, ctx: commands.Context):
        """Проверить права бота на всех серверах"""
        try:
            await ctx.send(embed=create_info_embed("⏳ Проверка прав...", "В процессе"), ephemeral=True)

            # Проверяем права на всех серверах
            validation_results = await validate_all_servers(self.bot)

            if not validation_results:
                await ctx.send(
                    embed=create_success_embed(
                        "✅ Все права в порядке на всех серверах!",
                        "Проверка прав завершена"
                    ),
                    ephemeral=True
                )
            else:
                # Формируем отчет о проблемах
                report = format_permissions_report(validation_results)

                # Если отчет слишком длинный, разбиваем на части
                if len(report) > 1900:
                    report = report[:1900] + "\n\n... (отчет обрезан, проверьте логи)"

                await ctx.send(
                    embed=create_error_embed(report, "Обнаружены проблемы с правами"),
                    ephemeral=True
                )

            logger.info(f"Проверка прав выполнена пользователем {ctx.author}")

        except Exception as e:
            logger.error(f"Ошибка проверки прав: {e}", exc_info=True)
            await ctx.send(embed=create_error_embed(f"Ошибка: {e}"), ephemeral=True)

    @role_admin.command(name="debug_user", description="Показать детальную информацию о ролях пользователя")
    @app_commands.describe(user_id="ID пользователя Discord (Discord Snowflake)")
    async def debug_user(self, ctx: commands.Context, user_id: str):
        """Показать детальную информацию о ролях пользователя на всех серверах"""
        try:
            # Преобразование в int
            try:
                user_id_int = int(user_id)
            except ValueError:
                await ctx.send(
                    embed=create_error_embed("ID пользователя должен быть числом (Discord Snowflake)."),
                    ephemeral=True
                )
                return

            # Получаем все сервера где есть пользователь
            mutual_guilds = await self.sync_engine.get_user_mutual_guilds(user_id_int)

            embed = discord.Embed(
                title=f"🔍 Диагностика пользователя {user_id_int}",
                color=0x3498db
            )

            # Главный сервер
            main_server_id = self.bot.config.get_main_server_id()
            main_guild = self.bot.get_guild(main_server_id)

            if main_guild:
                try:
                    main_member = await main_guild.fetch_member(user_id_int)
                    roles_text = []
                    for role in main_member.roles:
                        if not role.is_default():
                            roles_text.append(f"{role.mention} (`{role.id}`)")

                    embed.add_field(
                        name=f"👑 Главный сервер: {main_guild.name}",
                        value="\n".join(roles_text) if roles_text else "Нет ролей",
                        inline=False
                    )
                except:
                    embed.add_field(
                        name=f"👑 Главный сервер: {main_guild.name}",
                        value="❌ Пользователь не найден",
                        inline=False
                    )

            # Другие сервера
            for guild in mutual_guilds:
                try:
                    member = await guild.fetch_member(user_id_int)
                    roles_text = []
                    for role in member.roles:
                        if not role.is_default():
                            roles_text.append(f"{role.name} (`{role.id}`)")

                    embed.add_field(
                        name=f"🌐 {guild.name} (`{guild.id}`)",
                        value="\n".join(roles_text[:10]) if roles_text else "Нет ролей",
                        inline=False
                    )
                except:
                    continue

            # Проверяем маппинги
            user_roles_map = await self.sync_engine.get_user_roles_from_guilds(user_id_int, mutual_guilds)
            target_roles = await self.sync_engine.calculate_target_roles(user_roles_map)

            if target_roles and main_guild:
                target_text = []
                for rid in target_roles:
                    role = main_guild.get_role(rid)
                    target_text.append(role.mention if role else f"`{rid}`")
                embed.add_field(
                    name=f"🎯 Целевые роли ({len(target_roles)})",
                    value=", ".join(target_text),
                    inline=False
                )
            else:
                embed.add_field(
                    name="🎯 Целевые роли",
                    value="Нет маппингов для ролей этого пользователя",
                    inline=False
                )

            await ctx.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Ошибка диагностики пользователя: {e}", exc_info=True)
            await ctx.send(embed=create_error_embed(f"Ошибка: {e}"), ephemeral=True)

    @role_admin.command(name="autosync", description="Переключить автоматическую синхронизацию (вкл/выкл)")
    async def toggle_autosync(self, ctx: commands.Context):
        """Переключить автоматическую синхронизацию (вкл/выкл)"""
        # Находим RoleMonitorCog
        monitor_cog = self.bot.get_cog("RoleMonitorCog")
        if not monitor_cog:
            await ctx.send(
                embed=create_error_embed("Модуль мониторинга не загружен."),
                ephemeral=True
            )
            return

        is_running = monitor_cog.process_pending_syncs.is_running()

        if is_running:
            monitor_cog.process_pending_syncs.cancel()
            await ctx.send(
                embed=create_info_embed(
                    "Автоматическая синхронизация **отключена**.",
                    "Автосинхронизация"
                ),
                ephemeral=True
            )
            logger.info(f"Автосинхронизация отключена пользователем {ctx.author}")
        else:
            monitor_cog.process_pending_syncs.start()
            await ctx.send(
                embed=create_success_embed(
                    "Автоматическая синхронизация **включена**.",
                    "Автосинхронизация"
                ),
                ephemeral=True
            )
            logger.info(f"Автосинхронизация включена пользователем {ctx.author}")

    @role_admin.error
    async def role_admin_error(self, ctx: commands.Context, error: Exception):
        """Обработчик ошибок группы команд role_admin"""
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(
                embed=create_error_embed(
                    "У вас нет прав администратора для использования этой команды.",
                    "Недостаточно прав"
                ),
                ephemeral=True
            )
        else:
            logger.error(f"Ошибка в команде role_admin: {error}", exc_info=True)
            await ctx.send(embed=create_error_embed(f"Произошла ошибка: {error}"), ephemeral=True)


async def setup(bot):
    """
    Функция для загрузки Cog

    Args:
        bot: Объект бота
    """
    await bot.add_cog(AdminCommandsCog(bot))
    logger.info("AdminCommandsCog добавлен в бота")
