"""
Валидатор заявок на роли с учётом источника истины (source_of_truth) маппинга.

Решает судьбу заявки в зависимости от настроенного источника истины:
  - fraction_discord — правда = роль на фракционном Discord. Авто-одобрение, если
    у пользователя уже есть source-роль на фракционном сервере; иначе ручной режим.
  - forum            — правда = ранг на форуме фракции. Если форум подтверждает
    ранг — выдаём source-роль на фракционном сервере (движок зеркалит её на main),
    если нет — отказ; если форум недоступен/аккаунт не найден — ручной режим.
  - manual           — без автопроверки, всегда ручной режим.

ВАЖНО: при forum выдаём именно source-роль на фракционном (source) сервере, НЕ
целевую роль на main — иначе движок синхронизации снимет целевую роль при
следующем синке (sync_engine.calculate_role_changes).
"""

from enum import Enum
from dataclasses import dataclass

from bot.core.forum_provider import ForumProvider
from bot.utils.logger import get_logger

logger = get_logger("core.request_validator")


class Decision(Enum):
    AUTO_APPROVE = "auto_approve"  # выдать роль автоматически
    REJECT = "reject"              # отказать
    MANUAL = "manual"              # на ручное рассмотрение администратором


@dataclass
class ValidationResult:
    decision: Decision
    reason: str
    # Нужно ли выдать source-роль на фракционном сервере (для source_of_truth=forum)
    grant_source_role: bool = False


class RequestValidator:
    """Определяет решение по заявке исходя из источника истины маппинга."""

    def __init__(self, forum_provider: ForumProvider):
        self.forum = forum_provider

    async def validate(
        self,
        mapping,
        *,
        user_has_source_role: bool = False,
        forum_account: str = "",
    ) -> ValidationResult:
        """
        Args:
            mapping: RoleMapping (несёт source_of_truth и forum_rank)
            user_has_source_role: есть ли у пользователя source-роль на
                фракционном Discord (для source_of_truth=fraction_discord)
            forum_account: форумный аккаунт пользователя (для source_of_truth=forum)
        """
        sot = mapping.source_of_truth

        if sot == "manual":
            return ValidationResult(Decision.MANUAL, "Ручной режим проверки и выдачи")

        if sot == "fraction_discord":
            if user_has_source_role:
                return ValidationResult(
                    Decision.AUTO_APPROVE,
                    "Роль подтверждена на фракционном Discord",
                )
            return ValidationResult(
                Decision.MANUAL,
                "Роль не найдена на фракционном Discord — нужна ручная проверка",
            )

        if sot == "forum":
            return await self._validate_forum(mapping, forum_account)

        logger.warning(f"Неизвестный source_of_truth '{sot}' — ручной режим")
        return ValidationResult(Decision.MANUAL, f"Неизвестный источник истины: {sot}")

    async def _validate_forum(self, mapping, forum_account: str) -> ValidationResult:
        if not forum_account:
            return ValidationResult(
                Decision.MANUAL, "Не указан форумный аккаунт — ручная проверка"
            )
        if not mapping.forum_rank:
            return ValidationResult(
                Decision.MANUAL,
                "Для маппинга не задан forum_rank — ручная проверка",
            )

        try:
            ranks = await self.forum.get_member_ranks(forum_account)
        except Exception as e:
            logger.warning(f"Ошибка форума для {forum_account}: {e}")
            return ValidationResult(
                Decision.MANUAL, "Форум недоступен — ручная проверка"
            )

        # Пустой список рангов трактуем как «форум недоступен или аккаунт не найден»
        # → fail-safe в ручной режим, чтобы не отказывать легитимным игрокам.
        if not ranks:
            return ValidationResult(
                Decision.MANUAL,
                "Форум не вернул данных по аккаунту — ручная проверка",
            )

        wanted = mapping.forum_rank.strip().lower()
        if any(r.strip().lower() == wanted for r in ranks):
            return ValidationResult(
                Decision.AUTO_APPROVE,
                f"Ранг '{mapping.forum_rank}' подтверждён на форуме",
                grant_source_role=True,
            )

        return ValidationResult(
            Decision.REJECT,
            f"На форуме нет требуемого ранга '{mapping.forum_rank}'",
        )
