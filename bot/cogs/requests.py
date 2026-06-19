"""
Cog flow заявок на получение ролей:
  - регистрация постоянных view (кнопка + одобрение/отклонение);
  - автосоздание/ручное размещение кнопки в клиентском канале;
  - команда /search — история заявок пользователя.
"""

import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional

from bot.ui.requests import RequestButtonView, RequestActionView, _is_request_admin
from bot.utils.logger import get_logger

logger = get_logger("cogs.requests")


class RequestsCog(commands.Cog):
    """Cog приёма и обработки заявок на роли"""

    def __init__(self, bot):
        self.bot = bot
        self._ready_event_fired = False

    async def cog_load(self):
        logger.info("RequestsCog загружен")
        # Регистрируем постоянные view (переживают рестарт)
        self.bot.add_view(RequestButtonView(self.bot))
        self.bot.add_view(RequestActionView(self.bot))
        logger.info("Persistent views заявок зарегистрированы")

    @commands.Cog.listener()
    async def on_ready(self):
        if self._ready_event_fired:
            return
        self._ready_event_fired = True
        await self._auto_create_button()

    async def _auto_create_button(self):
        """Разместить кнопку 'Получить роли' в настроенном клиентском канале."""
        channel_id = self.bot.config.get_request_button_channel_id()
        if not channel_id:
            logger.info("Канал кнопки заявок не настроен — автосоздание пропущено")
            return

        channel = self.bot.get_channel(channel_id)
        if not channel:
            logger.warning(f"Канал кнопки заявок {channel_id} не найден")
            return

        try:
            embed = discord.Embed(
                title="Получение ролей",
                description=(
                    "Нажмите кнопку ниже, чтобы подать заявку на получение ролей.\n"
                    "Перед подачей необходимо состоять на сервере сообщества **САЕС**."
                ),
                color=discord.Color.blurple(),
            )
            await channel.send(embed=embed, view=RequestButtonView(self.bot))
            logger.info(f"Кнопка заявок размещена в канале {channel.name}")
        except discord.Forbidden:
            logger.error(f"Нет прав отправлять сообщения в канал {channel_id}")
        except Exception as e:
            logger.error(f"Ошибка автосоздания кнопки заявок: {e}", exc_info=True)

    @commands.hybrid_command(
        name="setup_request_button",
        description="Разместить кнопку подачи заявки на роли",
    )
    @commands.has_permissions(administrator=True)
    @app_commands.describe(channel="Канал для размещения (по умолчанию — текущий)")
    async def setup_request_button(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ):
        target = channel or ctx.channel
        embed = discord.Embed(
            title="Получение ролей",
            description=(
                "Нажмите кнопку ниже, чтобы подать заявку на получение ролей.\n"
                "Перед подачей необходимо состоять на сервере сообщества **САЕС**."
            ),
            color=discord.Color.blurple(),
        )
        try:
            await target.send(embed=embed, view=RequestButtonView(self.bot))
            await ctx.send(f"✅ Кнопка заявок размещена в {target.mention}", ephemeral=True)
        except discord.Forbidden:
            await ctx.send("❌ Нет прав для отправки в указанный канал.", ephemeral=True)

    @commands.hybrid_command(
        name="search", description="История заявок пользователя на роли"
    )
    @app_commands.describe(member="Пользователь, чьи заявки показать")
    async def search(self, ctx: commands.Context, member: discord.Member):
        # Только админы заявок
        if not _is_request_admin(self.bot, ctx.author):
            await ctx.send("❌ У вас нет прав для просмотра заявок.", ephemeral=True)
            return

        requests = await self.bot.db.get_requests_by_user(member.id)
        if not requests:
            await ctx.send(f"У {member.mention} нет заявок.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"Заявки: {member.display_name}",
            color=discord.Color.blurple(),
        )
        status_label = {
            "pending": "🟡 Ожидает",
            "approved": "🟢 Одобрена",
            "rejected": "🔴 Отклонена",
        }
        for req in requests[:10]:
            label = status_label.get(req["status"], req["status"])
            lines = [f"Статус: {label}", f"Создана: {req.get('created_at', '—')}"]
            if req.get("finished_by"):
                lines.append(f"Обработал: <@{req['finished_by']}>")
            if req.get("reject_reason"):
                lines.append(f"Причина: {req['reject_reason']}")
            embed.add_field(
                name=f"Заявка #{req['message_id']}",
                value="\n".join(lines),
                inline=False,
            )

        await ctx.send(embed=embed, ephemeral=True)

    @setup_request_button.error
    async def _setup_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ Нужны права администратора.", ephemeral=True)
        else:
            logger.error(f"Ошибка setup_request_button: {error}", exc_info=True)


async def setup(bot):
    await bot.add_cog(RequestsCog(bot))
    logger.info("RequestsCog добавлен в бота")
