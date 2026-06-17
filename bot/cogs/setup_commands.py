"""
Первоначальная настройка бота на главном сервере: /setup.

Позволяет администратору выбрать текстовый канал для audit-логов важных событий
ObjMapper (новый пользователь скрипта, обновление версии и т.п.). Выбор сохраняется
в bot_settings и сразу используется AuditLogger.
"""

import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional

from bot.core.audit import AUDIT_CHANNEL_KEY
from bot.ui.embeds import create_error_embed, COLOR_PRIMARY
from bot.utils.logger import get_logger

logger = get_logger("cogs.setup_commands")


AUDIT_CHANNEL_NAME = "objmapper-audit"


class _AuditChannelSelect(discord.ui.ChannelSelect):
    """Выбор СУЩЕСТВУЮЩЕГО текстового канала."""

    def __init__(self):
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder="Выбрать существующий канал…",
            min_values=1,
            max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        ch = self.values[0]
        await self.view.cog.bot.db.set_setting(AUDIT_CHANNEL_KEY, str(ch.id))
        logger.info(f"Audit-канал установлен: #{getattr(ch, 'name', ch.id)} ({ch.id}) "
                    f"админом {interaction.user}")
        await self.view.cog.send_test_message(ch.id)
        embed = await self.view.cog.build_setup_embed()
        await interaction.response.edit_message(embed=embed, view=self.view)


class _AuditCategorySelect(discord.ui.ChannelSelect):
    """Выбор КАТЕГОРИИ — бот сам создаст приватный канал внутри неё."""

    def __init__(self):
        super().__init__(
            channel_types=[discord.ChannelType.category],
            placeholder="…или выбрать категорию (создам приватный канал)",
            min_values=1,
            max_values=1,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        category_id = self.values[0].id
        ch = await self.view.cog.create_audit_channel(interaction, category_id)
        if ch is None:
            return  # ошибка уже показана в create_audit_channel
        embed = await self.view.cog.build_setup_embed()
        await interaction.response.edit_message(embed=embed, view=self.view)


class SetupView(discord.ui.View):
    def __init__(self, cog, author_id: int):
        super().__init__(timeout=180.0)
        self.cog = cog
        self.bot = cog.bot
        self.author_id = author_id
        self.message: Optional[discord.Message] = None
        self.add_item(_AuditChannelSelect())
        self.add_item(_AuditCategorySelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Это меню не для вас.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Отключить логи", style=discord.ButtonStyle.danger, row=2)
    async def disable(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.db.delete_setting(AUDIT_CHANNEL_KEY)
        logger.info(f"Audit-логи отключены админом {interaction.user}")
        embed = await self.cog.build_setup_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass
        self.stop()


class SetupCog(commands.Cog):
    """Первоначальная настройка бота."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        logger.info("SetupCog загружен")

    async def build_setup_embed(self) -> discord.Embed:
        cid = await self.bot.db.get_setting(AUDIT_CHANNEL_KEY)
        embed = discord.Embed(
            title="⚙️ Настройка бота — ObjMapper",
            description=(
                "Куда бот пишет **audit-логи** важных событий:\n"
                "• 🆕 новый пользователь · ⬆️ обновление версии · ⛔ потеря доступа\n"
                "• 🎭 выдача/снятие ролей (кнопка и авто-синхронизация)\n"
                "• 🧰 массовая синхронизация · 🗺 изменение маппингов · 🔁 тумблер автосинка\n\n"
                "**Выберите существующий канал** или **категорию** — во втором случае "
                "я сам создам приватный канал внутри неё."
            ),
            color=COLOR_PRIMARY,
        )
        if cid:
            embed.add_field(name="Канал audit-логов", value=f"<#{cid}>", inline=False)
            embed.set_footer(text="Логи включены. Можно сменить канал или отключить.")
        else:
            embed.add_field(name="Канал audit-логов", value="❌ не настроен", inline=False)
            embed.set_footer(text="Логи выключены, пока не выбран канал.")
        return embed

    async def create_audit_channel(self, interaction: discord.Interaction, category_id: int):
        """Создать ПРИВАТНЫЙ канал audit-логов в выбранной категории. None при ошибке прав."""
        guild = interaction.guild
        category = guild.get_channel(category_id)
        # Приватный по умолчанию: @everyone не видит, бот пишет.
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, embed_links=True
            ),
        }
        try:
            ch = await guild.create_text_channel(
                AUDIT_CHANNEL_NAME,
                category=category if isinstance(category, discord.CategoryChannel) else None,
                overwrites=overwrites,
                reason=f"ObjMapper: приватный канал audit-логов (создал {interaction.user})",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "Боту не хватает права **Управление каналами** (Manage Channels), "
                    "чтобы создать канал. Выдайте право или выберите существующий канал.",
                    "Недостаточно прав",
                ),
                ephemeral=True,
            )
            return None
        except Exception as e:  # noqa: BLE001
            await interaction.response.send_message(
                embed=create_error_embed(f"Не удалось создать канал: {e}"), ephemeral=True
            )
            return None

        await self.bot.db.set_setting(AUDIT_CHANNEL_KEY, str(ch.id))
        logger.info(f"Создан приватный audit-канал #{ch.name} ({ch.id}) "
                    f"в категории {category_id} админом {interaction.user}")
        await self.send_test_message(ch.id)
        return ch

    async def send_test_message(self, channel_id: int):
        """Подтверждение в выбранный канал (через AuditLogger → проверка прав/доступа)."""
        ch = self.bot.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(channel_id)
            except Exception:  # noqa: BLE001
                return
        embed = discord.Embed(
            title="✅ Канал назначен для audit-логов ObjMapper",
            description="Сюда будут приходить важные события (новые пользователи, обновления и т.д.).",
            color=COLOR_PRIMARY,
            timestamp=discord.utils.utcnow(),
        )
        try:
            await ch.send(embed=embed)
        except discord.Forbidden:
            logger.warning(f"Нет прав писать в выбранный audit-канал {channel_id}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Не удалось отправить тест в audit-канал: {e}")

    @commands.hybrid_command(name="setup", description="Первоначальная настройка бота (канал audit-логов)")
    @commands.has_permissions(administrator=True)
    async def setup_cmd(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        try:
            view = SetupView(self, ctx.author.id)
            embed = await self.build_setup_embed()
            msg = await ctx.send(embed=embed, view=view, ephemeral=True)
            view.message = msg
        except Exception as e:
            logger.error(f"setup error: {e}", exc_info=True)
            await ctx.send(embed=create_error_embed(f"Ошибка: {e}"), ephemeral=True)

    @setup_cmd.error
    async def setup_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(
                embed=create_error_embed("У вас нет прав для этой команды.", "Недостаточно прав"),
                ephemeral=True,
            )
        else:
            logger.error(f"Ошибка в команде setup: {error}", exc_info=True)
            await ctx.send(embed=create_error_embed(f"Произошла ошибка: {error}"), ephemeral=True)


async def setup(bot):
    await bot.add_cog(SetupCog(bot))
    logger.info("SetupCog добавлен в бота")
