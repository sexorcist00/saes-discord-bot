"""
Абстракция «источник истины — форум фракции».

Провайдер инкапсулирует доступ к фракционному форуму (реальный форум через
HTTP/парсинг). Конкретный парсинг изолирован в HttpForumProvider._parse_ranks,
чтобы под конкретную платформу форума можно было подменить только его.

Используется при source_of_truth == 'forum': проверяем, что у форумного аккаунта
есть нужный ранг, и только тогда выдаём роль.
"""

import abc
import time
import asyncio
from typing import List, Dict, Optional, Tuple

import aiohttp

from bot.utils.logger import get_logger

logger = get_logger("core.forum_provider")


class ForumProvider(abc.ABC):
    """Интерфейс провайдера форума."""

    @abc.abstractmethod
    async def get_member_ranks(self, forum_account: str) -> List[str]:
        """Вернуть список рангов/групп форумного аккаунта (пустой список — нет/ошибка)."""

    async def has_rank(self, forum_account: str, rank: str) -> bool:
        """Есть ли у аккаунта указанный ранг (без учёта регистра)."""
        if not forum_account or not rank:
            return False
        ranks = await self.get_member_ranks(forum_account)
        wanted = rank.strip().lower()
        return any(r.strip().lower() == wanted for r in ranks)

    async def close(self) -> None:
        """Освободить ресурсы (HTTP-сессию и т.п.)."""


class StubForumProvider(ForumProvider):
    """
    Заглушка: форум недоступен/выключен. Всегда возвращает пустой список рангов.

    При source_of_truth == 'forum' и этом провайдере валидатор уходит в fail-safe
    (ручное одобрение), а не выдаёт роль вслепую.
    """

    async def get_member_ranks(self, forum_account: str) -> List[str]:
        logger.debug("StubForumProvider: проверка форума пропущена (заглушка)")
        return []


class HttpForumProvider(ForumProvider):
    """
    Провайдер реального форума через HTTP с TTL-кэшем.

    Внимание: метод _parse_ranks зависит от вёрстки конкретного форума и должен
    быть доработан под реальную платформу (профиль/группы). По умолчанию
    возвращает [] и логирует предупреждение — это безопасный fail-safe.
    """

    def __init__(
        self,
        base_url: str,
        *,
        request_timeout: int = 10,
        cache_ttl: int = 300,
    ):
        self.base_url = base_url.rstrip("/")
        self.request_timeout = request_timeout
        self.cache_ttl = cache_ttl
        self._session: Optional[aiohttp.ClientSession] = None
        # forum_account -> (expires_at, ranks)
        self._cache: Dict[str, Tuple[float, List[str]]] = {}
        self._lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.request_timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    def _cache_get(self, account: str) -> Optional[List[str]]:
        entry = self._cache.get(account)
        if entry and entry[0] > time.monotonic():
            return entry[1]
        return None

    def _cache_put(self, account: str, ranks: List[str]) -> None:
        self._cache[account] = (time.monotonic() + self.cache_ttl, ranks)

    async def get_member_ranks(self, forum_account: str) -> List[str]:
        if not forum_account:
            return []

        cached = self._cache_get(forum_account)
        if cached is not None:
            return cached

        async with self._lock:
            # повторная проверка кэша под локом
            cached = self._cache_get(forum_account)
            if cached is not None:
                return cached
            try:
                html = await self._fetch_profile(forum_account)
                ranks = self._parse_ranks(html) if html else []
            except Exception as e:
                logger.warning(f"Ошибка запроса форума для {forum_account}: {e}")
                ranks = []
            self._cache_put(forum_account, ranks)
            return ranks

    async def _fetch_profile(self, forum_account: str) -> Optional[str]:
        """Загрузить HTML профиля форумного аккаунта."""
        session = await self._get_session()
        url = f"{self.base_url}/memberlist.php?username={forum_account}"
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.warning(f"Форум вернул {resp.status} для {url}")
                return None
            return await resp.text()

    def _parse_ranks(self, html: str) -> List[str]:
        """
        Извлечь ранги/группы из HTML профиля.

        TODO: реализовать под вёрстку конкретного форума фракции. Сейчас —
        безопасная заглушка: пусто (валидатор уйдёт в ручной режим).
        """
        logger.warning(
            "HttpForumProvider._parse_ranks не реализован под конкретный форум — "
            "возвращаю пустой список (fail-safe в ручной режим)"
        )
        return []

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


def create_forum_provider(config) -> ForumProvider:
    """
    Фабрика провайдера форума по конфигу.

    Возвращает StubForumProvider, если форум выключен или тип провайдера 'stub'.
    """
    if not config.is_forum_enabled():
        return StubForumProvider()

    provider_type = config.get_forum_provider_type()
    if provider_type == "http":
        return HttpForumProvider(
            base_url=config.get_forum_base_url(),
            request_timeout=config.get_forum_request_timeout(),
            cache_ttl=config.get_forum_cache_ttl(),
        )

    return StubForumProvider()
