"""
Тесты Фазы 4: провайдер форума и валидатор заявок (переключение источника истины).
"""

from dataclasses import dataclass

import pytest

from bot.core.forum_provider import StubForumProvider, create_forum_provider
from bot.core.request_validator import RequestValidator, Decision


@dataclass
class FakeMapping:
    source_of_truth: str = "manual"
    forum_rank: str = ""


class FakeForum:
    """Провайдер форума с заранее заданными рангами/ошибкой."""

    def __init__(self, ranks=None, raises=False):
        self._ranks = ranks or []
        self._raises = raises

    async def get_member_ranks(self, forum_account):
        if self._raises:
            raise RuntimeError("forum down")
        return list(self._ranks)


# ─────────────────────────── StubForumProvider ───────────────────────────


@pytest.mark.asyncio
async def test_stub_provider_returns_empty():
    p = StubForumProvider()
    assert await p.get_member_ranks("anyone") == []
    assert await p.has_rank("anyone", "Chief") is False


def test_factory_returns_stub_when_forum_disabled():
    cfg = type("C", (), {"is_forum_enabled": lambda self: False})()
    assert isinstance(create_forum_provider(cfg), StubForumProvider)


# ─────────────────────────── manual ───────────────────────────


@pytest.mark.asyncio
async def test_manual_always_manual():
    v = RequestValidator(FakeForum())
    res = await v.validate(FakeMapping(source_of_truth="manual"))
    assert res.decision == Decision.MANUAL


# ─────────────────────────── fraction_discord ───────────────────────────


@pytest.mark.asyncio
async def test_fraction_discord_auto_approve_when_has_role():
    v = RequestValidator(FakeForum())
    res = await v.validate(
        FakeMapping(source_of_truth="fraction_discord"),
        user_has_source_role=True,
    )
    assert res.decision == Decision.AUTO_APPROVE
    assert res.grant_source_role is False


@pytest.mark.asyncio
async def test_fraction_discord_manual_when_no_role():
    v = RequestValidator(FakeForum())
    res = await v.validate(
        FakeMapping(source_of_truth="fraction_discord"),
        user_has_source_role=False,
    )
    assert res.decision == Decision.MANUAL


# ─────────────────────────── forum ───────────────────────────


@pytest.mark.asyncio
async def test_forum_auto_approve_grants_source_role():
    v = RequestValidator(FakeForum(ranks=["Police Officer", "Chief of Police"]))
    res = await v.validate(
        FakeMapping(source_of_truth="forum", forum_rank="Chief of Police"),
        forum_account="JohnDoe",
    )
    assert res.decision == Decision.AUTO_APPROVE
    # критично: выдаём source-роль на фракционном сервере, не целевую
    assert res.grant_source_role is True


@pytest.mark.asyncio
async def test_forum_reject_when_rank_absent():
    v = RequestValidator(FakeForum(ranks=["Cadet"]))
    res = await v.validate(
        FakeMapping(source_of_truth="forum", forum_rank="Chief of Police"),
        forum_account="JohnDoe",
    )
    assert res.decision == Decision.REJECT


@pytest.mark.asyncio
async def test_forum_manual_when_no_ranks_returned():
    """Пустой ответ форума (недоступен/нет аккаунта) -> fail-safe в ручной режим."""
    v = RequestValidator(FakeForum(ranks=[]))
    res = await v.validate(
        FakeMapping(source_of_truth="forum", forum_rank="Chief of Police"),
        forum_account="JohnDoe",
    )
    assert res.decision == Decision.MANUAL


@pytest.mark.asyncio
async def test_forum_manual_on_provider_error():
    v = RequestValidator(FakeForum(raises=True))
    res = await v.validate(
        FakeMapping(source_of_truth="forum", forum_rank="Chief of Police"),
        forum_account="JohnDoe",
    )
    assert res.decision == Decision.MANUAL


@pytest.mark.asyncio
async def test_forum_manual_when_no_account():
    v = RequestValidator(FakeForum(ranks=["Chief of Police"]))
    res = await v.validate(
        FakeMapping(source_of_truth="forum", forum_rank="Chief of Police"),
        forum_account="",
    )
    assert res.decision == Decision.MANUAL
