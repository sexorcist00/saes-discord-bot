"""
Тесты логики членства (Фаза 2): снятие фракционных ролей при выходе с САЕС
и проверка членства на главном сервере.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.cogs.membership import MembershipCog, is_member_of_main


MAIN_SERVER_ID = 1000
FRACTION_SERVER_ID = 2000


def make_role(role_id: int, default: bool = False):
    role = MagicMock()
    role.id = role_id
    role.is_default.return_value = default
    return role


def make_bot(fraction_guilds, *, fraction_ids=None, mapped_roles=None):
    """Собрать мок-бота с конфигом, role_mapper и db."""
    mapped_roles = mapped_roles or set()

    bot = MagicMock()
    bot.config.get_main_server_id.return_value = MAIN_SERVER_ID
    bot.config.get_fraction_server_ids.return_value = fraction_ids or []
    bot.guilds = fraction_guilds
    # has_mapping(guild_id, role_id) -> role_id в множестве замапленных
    bot.role_mapper.has_mapping.side_effect = lambda gid, rid: rid in mapped_roles
    bot.db.log_sync_event = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_revoke_removes_only_mapped_manageable_roles():
    """Снимаются только замапленные роли, которыми бот может управлять."""
    mapped = make_role(50)       # есть маппинг -> снять
    unmapped = make_role(51)     # нет маппинга -> не трогать
    everyone = make_role(52, default=True)

    member = MagicMock()
    member.roles = [everyone, mapped, unmapped]
    member.remove_roles = AsyncMock()
    member.send = AsyncMock()

    guild = MagicMock()
    guild.id = FRACTION_SERVER_ID
    guild.name = "LSPD"
    guild.get_member.return_value = member

    bot = make_bot([guild], mapped_roles={50})
    cog = MembershipCog(bot)

    with patch(
        "bot.cogs.membership.get_manageable_roles",
        new=AsyncMock(return_value=([mapped], [])),
    ):
        removed = await cog.revoke_fraction_roles(user_id=777)

    assert removed is True
    member.remove_roles.assert_awaited_once()
    # Снята именно замапленная роль
    args, _ = member.remove_roles.call_args
    assert mapped in args and unmapped not in args
    bot.db.log_sync_event.assert_awaited()


@pytest.mark.asyncio
async def test_revoke_no_mapped_roles_does_nothing():
    """Если у пользователя нет замапленных ролей — ничего не снимаем."""
    member = MagicMock()
    member.roles = [make_role(51)]  # не замаплена
    member.remove_roles = AsyncMock()

    guild = MagicMock()
    guild.id = FRACTION_SERVER_ID
    guild.name = "LSPD"
    guild.get_member.return_value = member

    bot = make_bot([guild], mapped_roles=set())
    cog = MembershipCog(bot)

    removed = await cog.revoke_fraction_roles(user_id=777)

    assert removed is False
    member.remove_roles.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_member_remove_ignores_fraction_server():
    """Выход с фракционного сервера не запускает снятие ролей."""
    bot = make_bot([])
    cog = MembershipCog(bot)
    cog.revoke_fraction_roles = AsyncMock()

    member = MagicMock()
    member.bot = False
    member.guild.id = FRACTION_SERVER_ID  # не главный

    await cog.on_member_remove(member)

    cog.revoke_fraction_roles.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_member_remove_main_server_triggers_revoke_and_dm():
    """Выход с главного сервера снимает роли и шлёт DM."""
    bot = make_bot([])
    cog = MembershipCog(bot)
    cog.revoke_fraction_roles = AsyncMock(return_value=True)

    member = MagicMock()
    member.bot = False
    member.guild.id = MAIN_SERVER_ID
    member.id = 777
    member.send = AsyncMock()

    await cog.on_member_remove(member)

    cog.revoke_fraction_roles.assert_awaited_once_with(777)
    member.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_is_member_of_main_cache_hit():
    """get_member вернул участника — членство подтверждено без fetch."""
    main_guild = MagicMock()
    main_guild.get_member.return_value = MagicMock()
    main_guild.fetch_member = AsyncMock()

    bot = MagicMock()
    bot.config.get_main_server_id.return_value = MAIN_SERVER_ID
    bot.get_guild.return_value = main_guild

    assert await is_member_of_main(bot, 777) is True
    main_guild.fetch_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_is_member_of_main_not_found():
    """Участника нет ни в кеше, ни на сервере — членство не подтверждено."""
    import discord

    main_guild = MagicMock()
    main_guild.get_member.return_value = None
    main_guild.fetch_member = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "not found")
    )

    bot = MagicMock()
    bot.config.get_main_server_id.return_value = MAIN_SERVER_ID
    bot.get_guild.return_value = main_guild

    assert await is_member_of_main(bot, 777) is False
