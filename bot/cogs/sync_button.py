"""
Cog для обработки кнопки синхронизации ролей
"""

import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional

from bot.ui.buttons import SyncRolesView
from bot.ui.embeds import (
    create_sync_button_embed,
    create_sync_result_embed,
    create_processing_embed,
    create_error_embed
)
from bot.core.sync_engine import SyncEngine
from bot.utils.logger import get_logger
from bot.utils.errors import UserNotFoundError

logger = get_logger("cogs.sync_button")


class SyncButtonCog(commands.Cog):
    """Cog для управления кнопкой синхронизации"""

    def __init__(self, bot):
        """
        Инициализация Cog

        Args:
            bot: Объект бота
        """
        self.bot = bot
        self.sync_engine: Optional[SyncEngine] = None
        self._ready_event_fired = False  # Флаг для однократного выполнения

    async def cog_load(self):
        """Вызывается когда Cog загружается"""
        logger.info("SyncButtonCog загружен")

        # Используем общий RoleMapper бота
        self.role_mapper = self.bot.role_mapper

        self.sync_engine = SyncEngine(
            bot=self.bot,
            config=self.bot.config,
            db=self.bot.db,
            role_mapper=self.role_mapper
        )

        # Регистрируем persistent view
        view = SyncRolesView(self.bot)
        self.bot.add_view(view)
        logger.info("Persistent view для кнопки синхронизации зарегистрирован")

    @commands.Cog.listener()
    async def on_ready(self):
        """Вызывается когда бот полностью готов к работе"""
        # Выполняем только один раз
        if self._ready_event_fired:
            return
        self._ready_event_fired = True

        # Автоматически создаем кнопку
        await self._auto_create_sync_button()

    async def _auto_create_sync_button(self):
        """Автоматически создать кнопку синхронизации в настроенном канале"""
        # Получаем ID канала из конфига
        channel_id = self.bot.config.get_sync_button_channel_id()

        if not channel_id:
            logger.info("ID канала для кнопки синхронизации не настроен - автосоздание пропущено")
            return

        try:
            # Получаем канал
            channel = self.bot.get_channel(channel_id)
            if not channel:
                logger.warning(f"Канал с ID {channel_id} не найден для автосоздания кнопки")
                return

            # Создаем embed и view
            embed = create_sync_button_embed()
            view = SyncRolesView(self.bot)

            # Отправляем сообщение
            message = await channel.send(embed=embed, view=view)

            logger.info(
                f"Кнопка синхронизации автоматически создана в канале {channel.name} "
                f"(ID: {message.id})"
            )

        except discord.Forbidden:
            logger.error(f"Нет прав для отправки сообщений в канал {channel_id}")
        except Exception as e:
            logger.error(f"Ошибка автосоздания кнопки синхронизации: {e}", exc_info=True)

    @commands.hybrid_command(name="setup_sync_button", description="Создать сообщение с кнопкой синхронизации")
    @commands.has_permissions(administrator=True)
    @app_commands.describe(channel="Канал для размещения кнопки (по умолчанию - текущий)")
    async def setup_sync_button(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """Создать сообщение с кнопкой синхронизации"""
        target_channel = channel or ctx.channel

        try:
            # Создаем embed и view
            embed = create_sync_button_embed()
            view = SyncRolesView(self.bot)

            # Отправляем сообщение
            message = await target_channel.send(embed=embed, view=view)

            logger.info(
                f"Кнопка синхронизации создана в канале {target_channel.name} "
                f"(ID: {message.id}) пользователем {ctx.author}"
            )

            # Подтверждение
            await ctx.send(
                f"✅ Кнопка синхронизации создана в {target_channel.mention}",
                ephemeral=True
            )

        except discord.Forbidden:
            await ctx.send("❌ Нет прав для отправки сообщений в указанный канал.", ephemeral=True)
        except Exception as e:
            logger.error(f"Ошибка создания кнопки синхронизации: {e}", exc_info=True)
            await ctx.send(f"❌ Ошибка при создании кнопки: {e}", ephemeral=True)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """
        Обработчик всех взаимодействий (кнопок, меню и т.д.)

        Args:
            interaction: Объект взаимодействия
        """
        # Проверяем что это наша кнопка синхронизации
        if interaction.data.get("custom_id") != "role_sync_button":
            return

        # Проверяем что пользователь не бот
        if interaction.user.bot:
            await interaction.response.send_message(
                "❌ Боты не могут использовать синхронизацию ролей.",
                ephemeral=True
            )
            return

        logger.info(f"Пользователь {interaction.user} ({interaction.user.id}) нажал кнопку синхронизации")

        # Отправляем сообщение о начале обработки
        try:
            await interaction.response.send_message(
                embed=create_processing_embed(),
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения о начале обработки: {e}")
            return

        # Выполняем синхронизацию
        try:
            result = await self.sync_engine.sync_user_roles(
                user_id=interaction.user.id,
                trigger_type="button"
            )

            # Создаем embed с результатом
            result_embed = create_sync_result_embed(
                result=result,
                guild=interaction.guild,
                user=interaction.user
            )

            # Обновляем сообщение с результатом
            await interaction.edit_original_response(embed=result_embed)

            logger.info(
                f"Синхронизация для {interaction.user} завершена: "
                f"+{len(result.roles_added)}, -{len(result.roles_removed)}"
            )

        except UserNotFoundError as e:
            # Пользователь не найден на главном сервере
            error_embed = create_error_embed(
                error_message="Вы не состоите в главном сервере фракции",
                title="Ошибка доступа"
            )
            await interaction.edit_original_response(embed=error_embed)
            logger.warning(f"Пользователь {interaction.user.id} не найден на главном сервере")

        except Exception as e:
            # Общая ошибка
            error_embed = create_error_embed(
                error_message="Не удалось получить роли. Попробуйте позже",
                title="Ошибка"
            )
            await interaction.edit_original_response(embed=error_embed)
            logger.error(
                f"Ошибка синхронизации для {interaction.user}: {e}",
                exc_info=True
            )

    @setup_sync_button.error
    async def setup_sync_button_error(self, ctx: commands.Context, error: Exception):
        """Обработчик ошибок команды setup_sync_button"""
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ У вас нет прав администратора для использования этой команды.", ephemeral=True)
        else:
            logger.error(f"Ошибка в команде setup_sync_button: {error}", exc_info=True)
            await ctx.send(f"❌ Произошла ошибка: {error}", ephemeral=True)


async def setup(bot):
    """
    Функция для загрузки Cog

    Args:
        bot: Объект бота
    """
    await bot.add_cog(SyncButtonCog(bot))
    logger.info("SyncButtonCog добавлен в бота")
