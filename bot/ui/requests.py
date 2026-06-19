"""
UI flow заявок на получение ролей (порт из lspd-manager, адаптирован под
saes-discord-bot: SQLite через bot.db, каналы из конфига, gate членства).

Персистентность: все view имеют timeout=None и фиксированные custom_id и
регистрируются через bot.add_view(), поэтому переживают рестарт без
перевешивания на конкретные сообщения. Пользователь и статус заявки
восстанавливаются из БД по ID сообщения админ-канала.
"""

import discord
from datetime import datetime
from typing import Optional

from bot.cogs.membership import is_member_of_main
from bot.utils.logger import get_logger

logger = get_logger("ui.requests")


def _is_request_admin(bot, member: discord.Member) -> bool:
    """Может ли участник одобрять/отклонять заявки."""
    admin_role_ids = set(bot.config.get_request_admin_role_ids())
    if admin_role_ids:
        return any(r.id in admin_role_ids for r in getattr(member, "roles", []))
    # Фолбэк: право Administrator
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and perms.administrator)


# ─────────────────────────── Кнопка "Получить роли" ───────────────────────────


class RequestButtonView(discord.ui.View):
    """Постоянная кнопка для клиентского канала."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Получить роли",
        custom_id="request_open",
        style=discord.ButtonStyle.green,
    )
    async def open_request(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Gate: требуем членства на сервере САЕС перед подачей заявки
        if not await is_member_of_main(self.bot, interaction.user.id):
            await interaction.response.send_message(
                "❌ Чтобы подать заявку на роли, сначала вступите в Discord-сервер "
                "сообщества **САЕС**, затем повторите попытку.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(FeedbackModal(self.bot))


# ─────────────────────────── Модалка подачи заявки ───────────────────────────


class FeedbackModal(discord.ui.Modal, title="Получение роли"):
    info = discord.ui.TextInput(
        label="Информация",
        default="Обязательно укажите никнейм в формате: IC Nickname (OOC Nick).",
        style=discord.TextStyle.long,
        required=False,
    )
    feedback = discord.ui.TextInput(
        label="Укажите необходимые роли:",
        style=discord.TextStyle.long,
        required=True,
        max_length=300,
    )
    forum = discord.ui.TextInput(
        label="Форумный аккаунт фракции:",
        style=discord.TextStyle.short,
        placeholder="Профиль на форуме фракции",
        required=True,
        max_length=100,
    )
    vk = discord.ui.TextInput(
        label="Ваш ВКонтакте:",
        style=discord.TextStyle.short,
        placeholder="https://vk.com/id1",
        required=True,
        max_length=100,
    )

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        admin_channel_id = self.bot.config.get_request_admin_channel_id()
        channel = self.bot.get_channel(admin_channel_id) if admin_channel_id else None
        if channel is None:
            logger.error(f"Канал админ-ревью заявок не найден (id={admin_channel_id})")
            await interaction.response.send_message(
                "❌ Канал для заявок не настроен. Сообщите администрации.",
                ephemeral=True,
            )
            return

        user = interaction.user
        embed = discord.Embed(
            title="Новый запрос",
            description=(
                f"**От {user.mention}**\n\n"
                f"**{self.feedback.label}**\n{self.feedback.value}\n"
                f"**{self.forum.label}**\n{self.forum.value}\n"
                f"**{self.vk.label}**\n{self.vk.value}"
            ),
            color=discord.Color.yellow(),
        )
        embed.set_author(
            name=user.display_name,
            icon_url=user.display_avatar.url,
            url=f"https://discord.com/users/{user.id}",
        )

        message = await channel.send(embed=embed, view=RequestActionView(self.bot))

        try:
            await self.bot.db.create_request(
                message_id=message.id,
                user_id=user.id,
                embed=embed.to_dict(),
            )
        except Exception as e:
            logger.error(f"Ошибка сохранения заявки в БД: {e}", exc_info=True)

        await interaction.response.send_message(
            f"✅ Заявка отправлена, {user.mention}! Ожидайте решения администрации.",
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error(f"Ошибка модалки заявки: {error}", exc_info=True)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Упс! Что-то пошло не так.", ephemeral=True
            )


# ───────────────────── Кнопки одобрения/отклонения заявки ─────────────────────


class RequestActionView(discord.ui.View):
    """Постоянные кнопки 'Выполнено' / 'Отклонить' на сообщении заявки."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _resolve_user(self, message_id: int) -> Optional[int]:
        """Найти user_id заявки по ID сообщения админ-канала."""
        try:
            rows = await self.bot.db._fetchone(
                "SELECT user_id FROM requests WHERE message_id = ?", (message_id,)
            )
            return rows["user_id"] if rows else None
        except Exception as e:
            logger.error(f"Ошибка поиска заявки {message_id}: {e}", exc_info=True)
            return None

    @discord.ui.button(
        label="Выполнено", custom_id="request_done", style=discord.ButtonStyle.green
    )
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_request_admin(self.bot, interaction.user):
            await interaction.response.send_message(
                "❌ У вас нет прав для обработки заявок.", ephemeral=True
            )
            return

        message = interaction.message
        user_id = await self._resolve_user(message.id)

        embed = message.embeds[0] if message.embeds else discord.Embed()
        embed.color = discord.Color.green()
        embed.set_footer(text=f"Запрос выполнен: {interaction.user.display_name}")
        await message.edit(embed=embed, view=None)

        try:
            await self.bot.db.set_request_approved(message.id, interaction.user.id)
        except Exception as e:
            logger.error(f"Ошибка обновления статуса заявки: {e}", exc_info=True)

        await _dm_user(self.bot, user_id, "Ваш запрос на получение ролей был одобрен.")
        await interaction.response.send_message("✅ Заявка одобрена.", ephemeral=True)

    @discord.ui.button(
        label="Отклонить", custom_id="request_drop", style=discord.ButtonStyle.red
    )
    async def drop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_request_admin(self.bot, interaction.user):
            await interaction.response.send_message(
                "❌ У вас нет прав для обработки заявок.", ephemeral=True
            )
            return

        await interaction.response.send_modal(DropModal(self.bot, interaction.message))


class DropModal(discord.ui.Modal, title="Причина отказа"):
    reason = discord.ui.TextInput(
        label="Укажите причину отказа",
        style=discord.TextStyle.long,
        placeholder="Например, недостаточно информации",
        required=True,
        max_length=300,
    )

    def __init__(self, bot, message: discord.Message):
        super().__init__()
        self.bot = bot
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        user_id = None
        try:
            row = await self.bot.db._fetchone(
                "SELECT user_id FROM requests WHERE message_id = ?", (self.message.id,)
            )
            user_id = row["user_id"] if row else None
        except Exception as e:
            logger.error(f"Ошибка поиска заявки {self.message.id}: {e}", exc_info=True)

        embed = self.message.embeds[0] if self.message.embeds else discord.Embed()
        embed.color = discord.Color.red()
        embed.set_footer(
            text=f"Отклонено: {interaction.user.display_name}. Причина: {self.reason.value}"
        )
        await self.message.edit(embed=embed, view=None)

        try:
            await self.bot.db.set_request_rejected(
                self.message.id, interaction.user.id, self.reason.value
            )
        except Exception as e:
            logger.error(f"Ошибка обновления статуса заявки: {e}", exc_info=True)

        await _dm_user(
            self.bot,
            user_id,
            f"Ваш запрос на получение ролей был отклонён. Причина: {self.reason.value}",
        )
        await interaction.response.send_message("Заявка отклонена.", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error(f"Ошибка модалки отказа: {error}", exc_info=True)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Упс! Что-то пошло не так.", ephemeral=True
            )


async def _dm_user(bot, user_id: Optional[int], text: str):
    """Отправить DM пользователю по его id (best-effort)."""
    if not user_id:
        return
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(text)
    except discord.Forbidden:
        logger.info(f"Не удалось отправить DM пользователю {user_id} (закрыты ЛС)")
    except discord.HTTPException as e:
        logger.warning(f"Ошибка отправки DM пользователю {user_id}: {e}")
