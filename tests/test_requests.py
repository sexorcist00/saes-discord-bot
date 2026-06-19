"""
Тесты flow заявок на роли (Фаза 3): gate членства, одобрение, отклонение,
проверка прав админа заявок.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import bot.ui.requests as reqmod
from bot.ui.requests import (
    RequestButtonView,
    RequestActionView,
    DropModal,
    _is_request_admin,
)


def make_role(role_id):
    r = MagicMock()
    r.id = role_id
    return r


def make_bot(admin_role_ids=None):
    bot = MagicMock()
    bot.config.get_request_admin_role_ids.return_value = admin_role_ids or []
    bot.db.set_request_approved = AsyncMock()
    bot.db.set_request_rejected = AsyncMock()
    bot.db._fetchone = AsyncMock(return_value={"user_id": 777})
    dm_user = MagicMock()
    dm_user.send = AsyncMock()
    bot.get_user.return_value = dm_user
    return bot


def child_by_id(view, custom_id):
    return next(c for c in view.children if getattr(c, "custom_id", None) == custom_id)


# ─────────────────────────── _is_request_admin ───────────────────────────


def test_is_request_admin_by_role():
    bot = make_bot(admin_role_ids=[10])
    member = MagicMock()
    member.roles = [make_role(10)]
    assert _is_request_admin(bot, member) is True


def test_is_request_admin_denied():
    bot = make_bot(admin_role_ids=[10])
    member = MagicMock()
    member.roles = [make_role(99)]
    member.guild_permissions.administrator = False
    assert _is_request_admin(bot, member) is False


def test_is_request_admin_fallback_administrator():
    bot = make_bot(admin_role_ids=[])  # нет ролей -> фолбэк на Administrator
    member = MagicMock()
    member.roles = []
    member.guild_permissions.administrator = True
    assert _is_request_admin(bot, member) is True


# ─────────────────────────── Gate членства ───────────────────────────


@pytest.mark.asyncio
async def test_open_request_blocked_when_not_member():
    bot = make_bot()
    view = RequestButtonView(bot)
    btn = child_by_id(view, "request_open")

    interaction = MagicMock()
    interaction.user.id = 777
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()

    with patch.object(reqmod, "is_member_of_main", new=AsyncMock(return_value=False)):
        await btn.callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    interaction.response.send_modal.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_request_allows_member():
    bot = make_bot()
    view = RequestButtonView(bot)
    btn = child_by_id(view, "request_open")

    interaction = MagicMock()
    interaction.user.id = 777
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()

    with patch.object(reqmod, "is_member_of_main", new=AsyncMock(return_value=True)):
        await btn.callback(interaction)

    interaction.response.send_modal.assert_awaited_once()


# ─────────────────────────── Одобрение заявки ───────────────────────────


@pytest.mark.asyncio
async def test_done_approves_updates_db_and_dms():
    bot = make_bot(admin_role_ids=[10])
    view = RequestActionView(bot)
    btn = child_by_id(view, "request_done")

    embed = discord.Embed(title="Новый запрос")
    message = MagicMock()
    message.id = 555
    message.embeds = [embed]
    message.edit = AsyncMock()

    interaction = MagicMock()
    interaction.message = message
    interaction.user = MagicMock(display_name="Admin")
    interaction.user.roles = [make_role(10)]
    interaction.response.send_message = AsyncMock()

    await btn.callback(interaction)

    bot.db.set_request_approved.assert_awaited_once_with(555, interaction.user.id)
    message.edit.assert_awaited_once()
    assert embed.color == discord.Color.green()
    bot.get_user.return_value.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_done_denied_for_non_admin():
    bot = make_bot(admin_role_ids=[10])
    view = RequestActionView(bot)
    btn = child_by_id(view, "request_done")

    interaction = MagicMock()
    interaction.user.roles = [make_role(99)]
    interaction.user.guild_permissions.administrator = False
    interaction.response.send_message = AsyncMock()

    await btn.callback(interaction)

    bot.db.set_request_approved.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()


# ─────────────────────────── Отклонение заявки ───────────────────────────


@pytest.mark.asyncio
async def test_drop_modal_rejects_updates_db_and_dms():
    bot = make_bot()

    embed = discord.Embed(title="Новый запрос")
    message = MagicMock()
    message.id = 555
    message.embeds = [embed]
    message.edit = AsyncMock()

    modal = DropModal(bot, message)
    modal.reason._value = "недостаточно информации"

    interaction = MagicMock()
    interaction.user = MagicMock(display_name="Admin")
    interaction.response.send_message = AsyncMock()

    await modal.on_submit(interaction)

    bot.db.set_request_rejected.assert_awaited_once()
    args = bot.db.set_request_rejected.call_args.args
    assert args[0] == 555 and args[2] == "недостаточно информации"
    assert embed.color == discord.Color.red()
    bot.get_user.return_value.send.assert_awaited_once()
