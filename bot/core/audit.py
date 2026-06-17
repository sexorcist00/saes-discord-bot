"""
Audit-логгер ObjMapper: пишет важные события в выбранный через /setup канал.

Канал хранится в bot_settings (ключ AUDIT_CHANNEL_KEY). Если канал не настроен или
недоступен — событие тихо пропускается (логирование не должно ронять основной поток).
"""

import discord

from bot.utils.logger import get_logger

logger = get_logger("core.audit")

AUDIT_CHANNEL_KEY = "objmapper_audit_channel_id"

# Цвета событий
_COLOR_NEW = 0x2ecc71      # новый пользователь — зелёный
_COLOR_UPDATE = 0x3498db   # обновление — синий
_COLOR_WARN = 0xf39c12     # предупреждение — оранжевый


class AuditLogger:
    """Постит embed-события в настроенный audit-канал."""

    def __init__(self, bot):
        self.bot = bot

    async def get_channel_id(self):
        cid = await self.bot.db.get_setting(AUDIT_CHANNEL_KEY)
        if not cid:
            return None
        try:
            return int(cid)
        except (TypeError, ValueError):
            return None

    async def is_configured(self) -> bool:
        return (await self.get_channel_id()) is not None

    async def _resolve_channel(self):
        cid = await self.get_channel_id()
        if cid is None:
            return None
        ch = self.bot.get_channel(cid)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(cid)
            except Exception:  # noqa: BLE001
                return None
        return ch

    async def emit(self, embed: discord.Embed) -> bool:
        """Отправить embed в audit-канал. True если отправлено."""
        ch = await self._resolve_channel()
        if ch is None:
            return False
        try:
            await ch.send(embed=embed)
            return True
        except discord.Forbidden:
            logger.warning("Audit: нет прав писать в канал audit-логов")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Audit: не удалось отправить событие: {e}")
        return False

    # ── Готовые события ──

    @staticmethod
    def _mention(discord_user_id) -> str:
        return f"<@{discord_user_id}> (`{discord_user_id}`)" if discord_user_id else "—"

    async def new_user(self, discord_user_id, nick, roles=None):
        """Новый пользователь скрипта (первая привязка)."""
        embed = discord.Embed(
            title="🆕 Новый пользователь скрипта",
            color=_COLOR_NEW,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="SA-MP ник", value=f"`{nick}`", inline=True)
        embed.add_field(name="Discord", value=self._mention(discord_user_id), inline=True)
        if roles:
            names = ", ".join(r.get("name", "?") for r in roles) if isinstance(roles, list) else str(roles)
            if names:
                embed.add_field(name="Роли", value=names, inline=False)
        await self.emit(embed)

    async def script_updated(self, discord_user_id, nick, old_version, new_version):
        """Пользователь обновил скрипт (изменилась версия)."""
        embed = discord.Embed(
            title="⬆️ Обновление скрипта",
            description=f"`{old_version or '?'}` → **`{new_version}`**",
            color=_COLOR_UPDATE,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="SA-MP ник", value=f"`{nick}`", inline=True)
        embed.add_field(name="Discord", value=self._mention(discord_user_id), inline=True)
        await self.emit(embed)

    async def access_revoked(self, discord_user_id, nick, reason):
        """У ранее привязанного пользователя пропал доступ (роли/членство)."""
        embed = discord.Embed(
            title="⛔ Потерян доступ к скрипту",
            description=f"Причина: `{reason}`",
            color=_COLOR_WARN,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="SA-MP ник", value=f"`{nick}`", inline=True)
        embed.add_field(name="Discord", value=self._mention(discord_user_id), inline=True)
        await self.emit(embed)
