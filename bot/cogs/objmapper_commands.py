"""
Cog авторизации SAES Object Helper: выдача 6-значного токена привязки игрового скрипта.

Пользователь вызывает /objhelper link, получает одноразовый токен и вводит его в
скрипте. Скрипт обменивает токен на постоянный auth_token через HTTP API
(bot/api/server.py). Проверка членства в сервере и ролей происходит на стороне API
в момент обмена и при каждой валидации.
"""

import secrets
import time

import discord
from discord import app_commands
from discord.ext import commands

from bot.ui.embeds import create_info_embed, create_error_embed
from bot.utils.logger import get_logger

logger = get_logger("cogs.objmapper")


def _generate_token() -> str:
    """6-значный числовой токен (как в callout)."""
    return "".join(secrets.choice("0123456789") for _ in range(6))


class ObjMapperCog(commands.Cog):
    """Команды авторизации ObjMapper"""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        logger.info("ObjMapperCog загружен")

    objmapper = app_commands.Group(
        name="objhelper", description="Авторизация скрипта SAES Object Helper"
    )

    @objmapper.command(name="link", description="Получить токен для входа в SAES Object Helper")
    async def link(self, interaction: discord.Interaction):
        """Выдать одноразовый 6-значный токен привязки."""
        await interaction.response.defer(ephemeral=True)

        ttl = self.bot.config.get_objmapper_token_ttl()
        expires_at = int(time.time()) + ttl

        # Генерируем уникальный токен (с запасом попыток на случай коллизии).
        token = None
        for _ in range(10):
            candidate = _generate_token()
            existing = await self.bot.db.get_objmapper_token(candidate)
            if not existing:
                token = candidate
                break

        if token is None:
            await interaction.followup.send(
                embed=create_error_embed(
                    "Не удалось сгенерировать токен. Попробуйте ещё раз."
                ),
                ephemeral=True,
            )
            return

        try:
            await self.bot.db.create_objmapper_token(
                str(interaction.user.id), token, str(expires_at)
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"ObjMapper: ошибка сохранения токена: {e}", exc_info=True)
            await interaction.followup.send(
                embed=create_error_embed("Внутренняя ошибка. Попробуйте позже."),
                ephemeral=True,
            )
            return

        minutes = max(1, ttl // 60)
        embed = create_info_embed(
            message=(
                f"Ваш токен для входа в **SAES Object Helper**:\n\n"
                f"```\n{token}\n```\n"
                f"Введите его в окне авторизации скрипта (ник определится автоматически).\n"
                f"Токен действует **{minutes} мин** и работает один раз.\n\n"
                f"Никому не передавайте этот токен."
            ),
            title="🔑 SAES Object Helper — токен входа",
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info(f"ObjMapper: выдан токен пользователю {interaction.user.id}")


async def setup(bot):
    await bot.add_cog(ObjMapperCog(bot))
    logger.info("ObjMapperCog добавлен в бота")
