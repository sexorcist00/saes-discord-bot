"""
CRUD операции для работы с базой данных
"""

import json
import aiosqlite
from datetime import datetime, date
from typing import List, Dict, Optional, Tuple
from bot.utils.logger import get_logger
from bot.utils.errors import DatabaseError

logger = get_logger("database.operations")


class DatabaseOperations:
    """Класс для выполнения операций с базой данных"""

    def __init__(self, db_path: str):
        """
        Инициализация

        Args:
            db_path: Путь к файлу базы данных
        """
        self.db_path = db_path
        self._connection: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """Установить постоянное подключение к БД"""
        if self._connection is None:
            self._connection = await aiosqlite.connect(self.db_path)
            self._connection.row_factory = aiosqlite.Row
            # Оптимизации SQLite
            await self._connection.execute("PRAGMA journal_mode=WAL")
            await self._connection.execute("PRAGMA synchronous=NORMAL")
            await self._connection.execute("PRAGMA cache_size=10000")
            logger.info("Подключение к БД установлено")

    async def close(self) -> None:
        """Закрыть подключение к БД"""
        if self._connection:
            await self._connection.close()
            self._connection = None
            logger.info("Подключение к БД закрыто")

    async def _get_connection(self) -> aiosqlite.Connection:
        """Получить подключение (создать если не существует)"""
        if self._connection is None:
            await self.connect()
        return self._connection

    async def _execute(self, query: str, params: tuple = ()) -> aiosqlite.Cursor:
        """Выполнить SQL запрос"""
        try:
            db = await self._get_connection()
            cursor = await db.execute(query, params)
            await db.commit()
            return cursor
        except Exception as e:
            logger.error(f"Ошибка выполнения запроса: {e}", exc_info=True)
            raise DatabaseError(f"Database error: {e}")

    async def _fetchone(self, query: str, params: tuple = ()) -> Optional[aiosqlite.Row]:
        """Выполнить запрос и вернуть одну строку"""
        db = await self._get_connection()
        async with db.execute(query, params) as cursor:
            return await cursor.fetchone()

    async def _fetchall(self, query: str, params: tuple = ()) -> List[aiosqlite.Row]:
        """Выполнить запрос и вернуть все строки"""
        db = await self._get_connection()
        async with db.execute(query, params) as cursor:
            return await cursor.fetchall()

    # ============ Sync State Operations ============

    async def update_sync_state(self, user_id: int, main_server_id: int) -> None:
        """
        Обновить состояние синхронизации пользователя

        Args:
            user_id: ID пользователя
            main_server_id: ID главного сервера
        """
        query = """
        INSERT INTO sync_state (user_id, main_server_id, last_sync_timestamp, sync_count)
        VALUES (?, ?, CURRENT_TIMESTAMP, 1)
        ON CONFLICT(user_id, main_server_id) DO UPDATE SET
            last_sync_timestamp = CURRENT_TIMESTAMP,
            sync_count = sync_count + 1
        """
        await self._execute(query, (user_id, main_server_id))
        logger.debug(f"Обновлено состояние синхронизации для пользователя {user_id}")

    async def get_sync_state(self, user_id: int, main_server_id: int) -> Optional[Dict]:
        """Получить состояние синхронизации пользователя"""
        query = """
        SELECT * FROM sync_state
        WHERE user_id = ? AND main_server_id = ?
        """
        row = await self._fetchone(query, (user_id, main_server_id))
        return dict(row) if row else None

    # ============ Role Assignment Operations ============

    async def record_role_assignment(
        self,
        user_id: int,
        source_server_id: int,
        source_role_id: int,
        target_server_id: int,
        target_role_id: int,
        assignment_type: str
    ) -> None:
        """
        Записать назначение роли

        Args:
            user_id: ID пользователя
            source_server_id: ID исходного сервера
            source_role_id: ID исходной роли
            target_server_id: ID целевого сервера
            target_role_id: ID целевой роли
            assignment_type: Тип назначения (button/auto/manual)
        """
        query = """
        INSERT INTO role_assignments (
            user_id, source_server_id, source_role_id,
            target_server_id, target_role_id, assignment_type
        ) VALUES (?, ?, ?, ?, ?, ?)
        """
        await self._execute(query, (
            user_id, source_server_id, source_role_id,
            target_server_id, target_role_id, assignment_type
        ))
        logger.debug(f"Записано назначение роли для пользователя {user_id}")

    async def get_user_role_assignments(
        self,
        user_id: int,
        limit: int = 50
    ) -> List[Dict]:
        """
        Получить историю назначения ролей пользователя

        Args:
            user_id: ID пользователя
            limit: Максимальное количество записей

        Returns:
            Список назначений ролей
        """
        query = """
        SELECT * FROM role_assignments
        WHERE user_id = ?
        ORDER BY assigned_timestamp DESC
        LIMIT ?
        """
        rows = await self._fetchall(query, (user_id, limit))
        return [dict(row) for row in rows]

    # ============ Sync Logs Operations ============

    async def log_sync_event(
        self,
        user_id: int,
        action_type: str,
        trigger_type: str,
        success: bool,
        source_server_id: Optional[int] = None,
        source_role_id: Optional[int] = None,
        target_server_id: Optional[int] = None,
        target_role_id: Optional[int] = None,
        error_message: Optional[str] = None
    ) -> None:
        """
        Записать событие синхронизации в логи

        Args:
            user_id: ID пользователя
            action_type: Тип действия
            trigger_type: Триггер синхронизации
            success: Успешно ли выполнено
            source_server_id: ID исходного сервера
            source_role_id: ID исходной роли
            target_server_id: ID целевого сервера
            target_role_id: ID целевой роли
            error_message: Сообщение об ошибке
        """
        query = """
        INSERT INTO sync_logs (
            user_id, action_type, trigger_type, success,
            source_server_id, source_role_id,
            target_server_id, target_role_id, error_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        await self._execute(query, (
            user_id, action_type, trigger_type, success,
            source_server_id, source_role_id,
            target_server_id, target_role_id, error_message
        ))

    async def get_recent_logs(
        self,
        limit: int = 100,
        user_id: Optional[int] = None,
        action_type: Optional[str] = None
    ) -> List[Dict]:
        """
        Получить недавние логи

        Args:
            limit: Максимальное количество записей
            user_id: Фильтр по ID пользователя
            action_type: Фильтр по типу действия

        Returns:
            Список логов
        """
        query = "SELECT * FROM sync_logs WHERE 1=1"
        params = []

        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)

        if action_type:
            query += " AND action_type = ?"
            params.append(action_type)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = await self._fetchall(query, tuple(params))
        return [dict(row) for row in rows]

    # ============ Statistics Operations ============

    async def update_statistics(
        self,
        trigger_type: str,
        success: bool,
        roles_assigned: int,
        user_id: int
    ) -> None:
        """
        Обновить статистику за сегодня (оптимизировано - 1 запрос вместо 5-6)

        Args:
            trigger_type: Тип триггера синхронизации
            success: Успешно ли выполнено
            roles_assigned: Количество назначенных ролей
            user_id: ID пользователя
        """
        today = date.today().isoformat()

        # Определяем значения для инкремента
        button_inc = 1 if trigger_type == "button" else 0
        auto_inc = 1 if trigger_type == "auto" else 0
        manual_inc = 1 if trigger_type == "manual" else 0
        success_inc = 1 if success else 0
        failed_inc = 0 if success else 1

        # Один запрос вместо 5-6
        query = """
        INSERT INTO statistics (
            stat_date, total_syncs, button_syncs, auto_syncs, manual_syncs,
            successful_syncs, failed_syncs, unique_users_synced, total_roles_assigned
        ) VALUES (?, 1, ?, ?, ?, ?, ?, 0, ?)
        ON CONFLICT(stat_date) DO UPDATE SET
            total_syncs = total_syncs + 1,
            button_syncs = button_syncs + ?,
            auto_syncs = auto_syncs + ?,
            manual_syncs = manual_syncs + ?,
            successful_syncs = successful_syncs + ?,
            failed_syncs = failed_syncs + ?,
            total_roles_assigned = total_roles_assigned + ?
        """
        await self._execute(query, (
            today, button_inc, auto_inc, manual_inc, success_inc, failed_inc, roles_assigned,
            button_inc, auto_inc, manual_inc, success_inc, failed_inc, roles_assigned
        ))

    async def get_statistics_summary(self, days: int = 30) -> Dict:
        """
        Получить сводную статистику за период

        Args:
            days: Количество дней

        Returns:
            Словарь со статистикой
        """
        query = """
        SELECT
            SUM(total_syncs) as total_syncs,
            SUM(button_syncs) as button_syncs,
            SUM(auto_syncs) as auto_syncs,
            SUM(manual_syncs) as manual_syncs,
            SUM(successful_syncs) as successful_syncs,
            SUM(failed_syncs) as failed_syncs,
            SUM(total_roles_assigned) as total_roles_assigned
        FROM statistics
        WHERE stat_date >= date('now', ?)
        """
        row = await self._fetchone(query, (f'-{days} days',))
        return dict(row) if row else {}

    async def get_daily_statistics(self, days: int = 7) -> List[Dict]:
        """
        Получить ежедневную статистику

        Args:
            days: Количество дней

        Returns:
            Список статистики по дням
        """
        query = """
        SELECT * FROM statistics
        WHERE stat_date >= date('now', ?)
        ORDER BY stat_date DESC
        """
        rows = await self._fetchall(query, (f'-{days} days',))
        return [dict(row) for row in rows]

    # ============ Sync Sessions Operations ============

    async def record_sync_session(
        self,
        user_id: int,
        trigger_type: str,
        success: bool,
        roles_added: List[int],
        roles_removed: List[int],
        roles_failed: List[int],
        source_servers: List[int],
        errors: List[str]
    ) -> None:
        """
        Записать сессию синхронизации

        Args:
            user_id: ID пользователя
            trigger_type: Тип триггера (button/auto/manual)
            success: Успешно ли завершена
            roles_added: Список ID реально добавленных ролей
            roles_removed: Список ID удалённых ролей
            roles_failed: Список ID ролей которые не удалось выдать
            source_servers: Список ID серверов-источников
            errors: Список текстов ошибок
        """
        query = """
        INSERT INTO sync_sessions (
            user_id, trigger_type, success,
            roles_added, roles_removed, roles_failed,
            source_servers, errors
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        await self._execute(query, (
            user_id, trigger_type, success,
            json.dumps(roles_added),
            json.dumps(roles_removed),
            json.dumps(roles_failed),
            json.dumps(source_servers),
            json.dumps(errors)
        ))
        logger.debug(f"Записана сессия синхронизации для пользователя {user_id}")

    async def get_recent_sync_sessions(
        self,
        limit: int = 50,
        user_id: Optional[int] = None
    ) -> List[Dict]:
        """
        Получить недавние сессии синхронизации

        Args:
            limit: Максимальное количество записей
            user_id: Фильтр по пользователю (опционально)

        Returns:
            Список сессий с распарсенными JSON полями
        """
        query = "SELECT * FROM sync_sessions WHERE 1=1"
        params = []

        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = await self._fetchall(query, tuple(params))

        sessions = []
        for row in rows:
            session = dict(row)
            # Парсим JSON поля
            for field in ('roles_added', 'roles_removed', 'roles_failed', 'source_servers', 'errors'):
                try:
                    session[field] = json.loads(session[field])
                except (json.JSONDecodeError, TypeError):
                    session[field] = []
            sessions.append(session)

        return sessions

    # ============ Role Mapping Cache Operations ============

    async def cache_role_mapping(
        self,
        mapping_id: str,
        source_server_id: int,
        source_role_id: int,
        target_server_id: int,
        target_role_id: int,
        enabled: bool = True,
        description: str = ""
    ) -> None:
        """
        Кешировать маппинг роли

        Args:
            mapping_id: ID маппинга
            source_server_id: ID исходного сервера
            source_role_id: ID исходной роли
            target_server_id: ID целевого сервера
            target_role_id: ID целевой роли
            enabled: Включен ли маппинг
            description: Описание
        """
        query = """
        INSERT INTO role_mapping_cache (
            mapping_id, source_server_id, source_role_id,
            target_server_id, target_role_id, enabled, description
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(mapping_id) DO UPDATE SET
            source_server_id = ?,
            source_role_id = ?,
            target_server_id = ?,
            target_role_id = ?,
            enabled = ?,
            description = ?,
            last_updated = CURRENT_TIMESTAMP
        """
        await self._execute(query, (
            mapping_id, source_server_id, source_role_id,
            target_server_id, target_role_id, enabled, description,
            source_server_id, source_role_id, target_server_id,
            target_role_id, enabled, description
        ))

    async def get_target_role(
        self,
        source_server_id: int,
        source_role_id: int
    ) -> Optional[int]:
        """
        Получить целевую роль для данной исходной роли

        Args:
            source_server_id: ID исходного сервера
            source_role_id: ID исходной роли

        Returns:
            ID целевой роли или None
        """
        query = """
        SELECT target_role_id FROM role_mapping_cache
        WHERE source_server_id = ? AND source_role_id = ? AND enabled = 1
        """
        row = await self._fetchone(query, (source_server_id, source_role_id))
        return row['target_role_id'] if row else None

    async def get_all_mappings(self) -> List[Dict]:
        """
        Получить все маппинги ролей

        Returns:
            Список всех маппингов
        """
        query = "SELECT * FROM role_mapping_cache ORDER BY mapping_id"
        rows = await self._fetchall(query)
        return [dict(row) for row in rows]

    async def remove_mapping(self, mapping_id: str) -> bool:
        """
        Удалить маппинг роли

        Args:
            mapping_id: ID маппинга

        Returns:
            True если удален успешно
        """
        query = "DELETE FROM role_mapping_cache WHERE mapping_id = ?"
        await self._execute(query, (mapping_id,))
        logger.info(f"Удален маппинг {mapping_id}")
        return True

    async def clear_mapping_cache(self) -> None:
        """Очистить весь кеш маппингов"""
        query = "DELETE FROM role_mapping_cache"
        await self._execute(query)
        logger.info("Кеш маппингов очищен")
