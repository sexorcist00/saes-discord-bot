"""
Тесты стабильности автосинхронизации:
  - подавление ложного снятия ролей при неполных данных с source-серверов;
  - повторные попытки в мониторе при транзиентных сбоях.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bot.core.sync_engine as sync_engine_mod
from bot.core.sync_engine import SyncEngine
from bot.cogs.role_monitor import RoleMonitorCog


MAIN = 1000
SOURCE = 2000
TARGET_ROLE = 200


def make_role(role_id, default=False, name="Role"):
    r = MagicMock()
    r.id = role_id
    r.is_default.return_value = default
    r.name = name
    return r


def make_engine(main_member, *, roles_map, fetch_errors):
    """SyncEngine с мок-окружением и подменённым чтением source-серверов."""
    main_guild = MagicMock()
    main_guild.id = MAIN
    main_guild.fetch_member = AsyncMock(return_value=main_member)

    bot = MagicMock()
    bot.get_guild.return_value = main_guild
    bot.guilds = [main_guild]

    config = MagicMock()
    config.get_main_server_id.return_value = MAIN

    db = MagicMock()
    for m in ("log_sync_event", "update_sync_state", "update_statistics",
              "record_sync_session", "record_role_assignment"):
        setattr(db, m, AsyncMock())

    role_mapper = MagicMock()
    role_mapper.get_all_target_roles.return_value = []  # нет подтверждённых целевых ролей
    role_mapper.is_target_role.side_effect = lambda rid: rid == TARGET_ROLE
    role_mapper.get_target_role.return_value = None

    engine = SyncEngine(bot=bot, config=config, db=db, role_mapper=role_mapper)
    # Подменяем чтение source-серверов (имитация транзиентного сбоя)
    source_guild = MagicMock()
    source_guild.id = SOURCE
    engine.get_user_roles_from_all_guilds = AsyncMock(
        return_value=([source_guild], roles_map, fetch_errors)
    )
    return engine


@pytest.mark.asyncio
async def test_removal_suppressed_on_incomplete_data():
    """Транзиентная ошибка чтения source-сервера НЕ должна снимать целевую роль."""
    target_role = make_role(TARGET_ROLE, name="Target")
    everyone = make_role(999, default=True, name="@everyone")

    main_member = MagicMock()
    main_member.id = 777
    main_member.roles = [everyone, target_role]  # уже имеет целевую роль
    main_member.guild = MagicMock(id=MAIN)
    main_member.add_roles = AsyncMock()
    main_member.remove_roles = AsyncMock()

    engine = make_engine(
        main_member,
        roles_map={},                 # со «сломанного» сервера роли не получены
        fetch_errors=["timeout на source-сервере"],
    )

    with patch.object(
        sync_engine_mod,
        "get_manageable_roles",
        new=AsyncMock(return_value=([target_role], [])),
    ):
        result = await engine.sync_user_roles(user_id=777, trigger_type="auto")

    # Критично: роль НЕ снята, несмотря на отсутствие оправдания (данные неполные)
    main_member.remove_roles.assert_not_awaited()
    assert result.data_incomplete is True
    assert result.success is False


@pytest.mark.asyncio
async def test_removal_proceeds_when_data_complete():
    """Без ошибок чтения неоправданная целевая роль снимается штатно."""
    target_role = make_role(TARGET_ROLE, name="Target")
    everyone = make_role(999, default=True, name="@everyone")

    main_member = MagicMock()
    main_member.id = 777
    main_member.roles = [everyone, target_role]
    main_member.guild = MagicMock(id=MAIN)
    main_member.add_roles = AsyncMock()
    main_member.remove_roles = AsyncMock()

    engine = make_engine(
        main_member,
        roles_map={},          # пользователя реально нет нужных ролей на source
        fetch_errors=[],        # данные полные
    )

    with patch.object(
        sync_engine_mod,
        "get_manageable_roles",
        new=AsyncMock(return_value=([target_role], [])),
    ):
        result = await engine.sync_user_roles(user_id=777, trigger_type="auto")

    # Данные полные -> неоправданная целевая роль снимается
    main_member.remove_roles.assert_awaited_once()
    assert result.data_incomplete is False


# ─────────────────────────── Повторы в мониторе ───────────────────────────


def test_maybe_retry_bounded():
    cog = RoleMonitorCog(MagicMock())
    cog.max_retries = 3

    # Первые 3 попытки ставят пользователя обратно в очередь
    for n in (1, 2, 3):
        cog.pending_syncs.clear()
        cog._maybe_retry(777)
        assert cog.retry_counts[777] == n
        assert 777 in cog.pending_syncs

    # 4-я попытка превышает лимит: не планируем и сбрасываем счётчик
    cog.pending_syncs.clear()
    cog._maybe_retry(777)
    assert 777 not in cog.pending_syncs
    assert 777 not in cog.retry_counts
