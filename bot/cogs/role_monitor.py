"""
Cog для мониторинга изменений ролей и автоматической синхронизации
"""

import discord
from discord.ext import commands, tasks
import asyncio
from typing import Dict, Optional
from datetime import datetime

from bot.core.sync_engine import SyncEngine
from bot.core.role_mapper import RoleMapper
from bot.utils.logger import get_logger

logger = get_logger("cogs.role_monitor")


class RoleMonitorCog(commands.Cog):
    """Cog для отслеживания изменений ролей на серверах"""

    def __init__(self, bot):
        """
        Инициализация Cog

        Args:
            bot: Объект бота
        """
        self.bot = bot
        self.sync_engine: Optional[SyncEngine] = None
        self.role_mapper: Optional[RoleMapper] = None

        # Очередь пользователей для синхронизации с debounce
        # Формат: {user_id: timestamp_последнего_изменения}
        self.pending_syncs: Dict[int, datetime] = {}

        # Задержка перед синхронизацией (секунды)
        self.debounce_delay = 5

    async def cog_load(self):
        """Вызывается когда Cog загружается"""
        logger.info("RoleMonitorCog загружен")

        # Создаем RoleMapper и SyncEngine
        self.role_mapper = RoleMapper(self.bot.config, self.bot.db)
        await self.role_mapper.initialize()

        self.sync_engine = SyncEngine(
            bot=self.bot,
            config=self.bot.config,
            db=self.bot.db,
            role_mapper=self.role_mapper
        )

        # Запускаем фоновую задачу обработки очереди
        if self.bot.config.is_auto_sync_enabled():
            self.process_pending_syncs.start()
            logger.info("Автоматическая синхронизация включена")
        else:
            logger.info("Автоматическая синхронизация отключена в конфигурации")

    async def cog_unload(self):
        """Вызывается при выгрузке Cog"""
        self.process_pending_syncs.cancel()
        logger.info("RoleMonitorCog выгружен")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """
        Событие: обновление информации о пользователе

        Args:
            before: Состояние до изменения
            after: Состояние после изменения
        """
        # Игнорируем если автосинхронизация отключена
        if not self.bot.config.is_auto_sync_enabled():
            return

        # Игнорируем ботов
        if after.bot:
            return

        # Игнорируем изменения на главном сервере
        # (мы только читаем роли с других серверов)
        main_server_id = self.bot.config.get_main_server_id()
        if after.guild.id == main_server_id:
            return

        # Проверяем изменились ли роли
        roles_before = set(role.id for role in before.roles)
        roles_after = set(role.id for role in after.roles)

        if roles_before == roles_after:
            return  # Роли не изменились

        # Вычисляем какие роли добавлены/удалены
        added_roles = roles_after - roles_before
        removed_roles = roles_before - roles_after

        # Проверяем есть ли измененные роли в наших маппингах
        has_mapped_changes = False

        for role_id in added_roles | removed_roles:
            if self.role_mapper.has_mapping(after.guild.id, role_id):
                has_mapped_changes = True
                break

        if not has_mapped_changes:
            logger.debug(
                f"Роли пользователя {after.id} изменились на сервере {after.guild.name}, "
                f"но ни одна из измененных ролей не в маппингах"
            )
            return

        # Логируем изменение
        logger.info(
            f"Обнаружено изменение ролей пользователя {after.display_name} ({after.id}) "
            f"на сервере {after.guild.name}: +{len(added_roles)}, -{len(removed_roles)}"
        )

        # Добавляем в очередь на синхронизацию
        await self.schedule_sync(after.id)

    async def schedule_sync(self, user_id: int):
        """
        Запланировать синхронизацию для пользователя с debounce

        Args:
            user_id: ID пользователя
        """
        # Обновляем timestamp последнего изменения
        self.pending_syncs[user_id] = datetime.now()

        logger.debug(
            f"Пользователь {user_id} добавлен в очередь синхронизации "
            f"(задержка {self.debounce_delay} сек)"
        )

    @tasks.loop(seconds=2)
    async def process_pending_syncs(self):
        """
        Фоновая задача обработки очереди синхронизации
        Выполняется каждые 2 секунды
        """
        if not self.pending_syncs:
            return

        now = datetime.now()
        users_to_sync = []

        # Находим пользователей готовых к синхронизации
        for user_id, last_change in list(self.pending_syncs.items()):
            time_since_change = (now - last_change).total_seconds()

            if time_since_change >= self.debounce_delay:
                users_to_sync.append(user_id)
                del self.pending_syncs[user_id]

        # Синхронизируем пользователей
        for user_id in users_to_sync:
            try:
                logger.info(f"Автоматическая синхронизация для пользователя {user_id}")

                result = await self.sync_engine.sync_user_roles(
                    user_id=user_id,
                    trigger_type="auto"
                )

                if result.success:
                    logger.info(
                        f"Автосинхронизация для {user_id} успешна: "
                        f"+{len(result.roles_added)}, -{len(result.roles_removed)}"
                    )
                else:
                    logger.warning(
                        f"Автосинхронизация для {user_id} завершена с ошибками: "
                        f"{result.errors}"
                    )

            except Exception as e:
                logger.error(
                    f"Ошибка автосинхронизации для пользователя {user_id}: {e}",
                    exc_info=True
                )

            # Небольшая задержка между синхронизациями
            await asyncio.sleep(0.5)

    @process_pending_syncs.before_loop
    async def before_process_pending_syncs(self):
        """Ожидание готовности бота перед запуском задачи"""
        await self.bot.wait_until_ready()



async def setup(bot):
    """
    Функция для загрузки Cog

    Args:
        bot: Объект бота
    """
    await bot.add_cog(RoleMonitorCog(bot))
    logger.info("RoleMonitorCog добавлен в бота")
