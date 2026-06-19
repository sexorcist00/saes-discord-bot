"""
Cog членства на сервере САЕС:
  - снятие ролей на фракционных серверах при выходе игрока с главного сервера САЕС
    + уведомление в личные сообщения;
  - хелпер проверки членства (gate перед подачей заявки на роли).
"""

import discord
from discord.ext import commands
from typing import List, Optional

from bot.core.permissions import get_manageable_roles
from bot.utils.logger import get_logger

logger = get_logger("cogs.membership")


# Текст DM при снятии ролей из-за выхода с сервера САЕС
LEFT_SERVER_DM = (
    "Вы покинули Discord-сервер сообщества **САЕС**, поэтому ваши роли на "
    "фракционных серверах были автоматически сняты.\n\n"
    "Чтобы вернуть роли — вступите обратно на сервер САЕС и подайте заявку заново."
)


async def is_member_of_main(bot: discord.Client, user_id: int) -> bool:
    """
    Состоит ли пользователь на главном сервере САЕС.

    Сначала проверяет кеш (get_member), при промахе делает fetch_member.

    Args:
        bot: Объект бота (должен иметь .config)
        user_id: ID пользователя Discord

    Returns:
        True если пользователь на главном сервере, иначе False
    """
    main_server_id = bot.config.get_main_server_id()
    main_guild = bot.get_guild(main_server_id)
    if main_guild is None:
        logger.warning(f"Главный сервер {main_server_id} недоступен для проверки членства")
        return False

    if main_guild.get_member(user_id) is not None:
        return True

    try:
        await main_guild.fetch_member(user_id)
        return True
    except discord.NotFound:
        return False
    except discord.HTTPException as e:
        logger.warning(f"Ошибка проверки членства пользователя {user_id}: {e}")
        return False


class MembershipCog(commands.Cog):
    """Отслеживание членства на сервере САЕС и снятие фракционных ролей при выходе"""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        logger.info("MembershipCog загружен")

    def _get_fraction_guilds(self) -> List[discord.Guild]:
        """
        Список фракционных серверов (source-серверы).

        Если в конфиге задан явный fraction_server_ids — используем его,
        иначе фракционными считаем все сервера бота, кроме главного.
        """
        main_server_id = self.bot.config.get_main_server_id()
        configured = set(self.bot.config.get_fraction_server_ids())

        if configured:
            return [g for g in self.bot.guilds if g.id in configured]
        return [g for g in self.bot.guilds if g.id != main_server_id]

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """
        Событие: участник покинул сервер (или был забанен/кикнут).

        Реагируем только на выход с ГЛАВНОГО сервера САЕС: снимаем роли-источники
        на всех фракционных серверах и шлём DM.
        """
        if member.bot:
            return

        main_server_id = self.bot.config.get_main_server_id()
        if member.guild.id != main_server_id:
            # Выход с фракционного сервера уже обрабатывается движком синхронизации
            return

        logger.info(
            f"Пользователь {member.display_name} ({member.id}) покинул сервер САЕС — "
            f"снимаю роли на фракционных серверах"
        )

        removed_any = await self.revoke_fraction_roles(member.id)

        # DM пользователю — только если реально что-то сняли
        if removed_any:
            await self._notify_user(member)

    async def revoke_fraction_roles(self, user_id: int) -> bool:
        """
        Снять у пользователя все управляемые роли-источники на фракционных серверах.

        Args:
            user_id: ID пользователя Discord

        Returns:
            True если хотя бы на одном сервере роли были сняты
        """
        removed_any = False

        for guild in self._get_fraction_guilds():
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.NotFound:
                    continue
                except discord.HTTPException as e:
                    logger.warning(f"Не удалось получить участника {user_id} на {guild.name}: {e}")
                    continue

            # Роли участника, для которых есть маппинг (source-роли)
            mapped_role_ids = [
                role.id for role in member.roles
                if not role.is_default() and self.bot.role_mapper.has_mapping(guild.id, role.id)
            ]
            if not mapped_role_ids:
                continue

            # Оставляем только те, которыми бот реально может управлять
            manageable, unmanageable = await get_manageable_roles(guild, mapped_role_ids)
            if unmanageable:
                logger.warning(
                    f"На {guild.name} не могу снять {len(unmanageable)} ролей "
                    f"(иерархия/права): {unmanageable}"
                )
            if not manageable:
                continue

            try:
                await member.remove_roles(
                    *manageable,
                    reason="Игрок покинул Discord-сервер САЕС"
                )
                removed_any = True
                logger.info(
                    f"Сняты {len(manageable)} ролей у {user_id} на сервере {guild.name}"
                )
                for role in manageable:
                    await self._log_removal(user_id, guild.id, role.id)
            except discord.Forbidden:
                logger.error(f"Нет прав снять роли у {user_id} на сервере {guild.name}")
            except discord.HTTPException as e:
                logger.error(f"Ошибка снятия ролей у {user_id} на {guild.name}: {e}")

        return removed_any

    async def _log_removal(self, user_id: int, guild_id: int, role_id: int):
        """Записать снятие роли в sync_logs"""
        try:
            await self.bot.db.log_sync_event(
                user_id=user_id,
                action_type="role_removed",
                trigger_type="auto",
                success=True,
                target_server_id=guild_id,
                target_role_id=role_id,
            )
        except Exception as e:
            logger.error(f"Ошибка логирования снятия роли: {e}", exc_info=True)

    async def _notify_user(self, user: discord.abc.User):
        """Отправить пользователю DM о снятии ролей"""
        try:
            await user.send(LEFT_SERVER_DM)
        except discord.Forbidden:
            logger.info(f"Не удалось отправить DM пользователю {user.id} (закрыты ЛС)")
        except discord.HTTPException as e:
            logger.warning(f"Ошибка отправки DM пользователю {user.id}: {e}")


async def setup(bot):
    await bot.add_cog(MembershipCog(bot))
    logger.info("MembershipCog добавлен в бота")
