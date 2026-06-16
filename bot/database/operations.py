"""
CRUD операции для работы с базой данных
"""

import json
import asyncio
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
        # Сериализует операции записи (execute + commit), чтобы commit одной
        # корутины не зафиксировал незавершённую запись другой.
        self._write_lock = asyncio.Lock()

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
            async with self._write_lock:
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

    # ============ Batch Operations ============

    async def execute_batch(self, operations: List[tuple]) -> None:
        """
        Выполнить пакет операций в одной транзакции (один commit).

        Args:
            operations: Список кортежей (op_type, params)
                op_type: 'log_sync_event', 'record_role_assignment',
                         'update_sync_state', 'update_statistics', 'record_sync_session'
        """
        if not operations:
            return

        db = await self._get_connection()

        try:
            async with self._write_lock:
                await self._run_batch_operations(db, operations)
            logger.info(f"Пакетная операция: выполнено {len(operations)} запросов")

        except Exception as e:
            logger.error(f"Ошибка пакетной операции БД: {e}", exc_info=True)
            try:
                await db.rollback()
            except Exception:
                pass
            raise DatabaseError(f"Batch database error: {e}")

    async def _run_batch_operations(self, db: aiosqlite.Connection, operations: List[tuple]) -> None:
        """Выполнить операции пакета внутри уже захваченного write-lock."""
        for op_type, params in operations:
                if op_type == "log_sync_event":
                    user_id, action_type, trigger_type, success, *rest = params
                    target_server_id = rest[0] if len(rest) > 0 else None
                    target_role_id = rest[1] if len(rest) > 1 else None
                    error_message = rest[2] if len(rest) > 2 else None
                    await db.execute(
                        """INSERT INTO sync_logs (
                            user_id, action_type, trigger_type, success,
                            source_server_id, source_role_id,
                            target_server_id, target_role_id, error_message
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (user_id, action_type, trigger_type, success,
                         None, None, target_server_id, target_role_id, error_message)
                    )

                elif op_type == "record_role_assignment":
                    await db.execute(
                        """INSERT INTO role_assignments (
                            user_id, source_server_id, source_role_id,
                            target_server_id, target_role_id, assignment_type
                        ) VALUES (?, ?, ?, ?, ?, ?)""",
                        params
                    )

                elif op_type == "update_sync_state":
                    user_id, main_server_id = params
                    await db.execute(
                        """INSERT INTO sync_state (user_id, main_server_id, last_sync_timestamp, sync_count)
                        VALUES (?, ?, CURRENT_TIMESTAMP, 1)
                        ON CONFLICT(user_id, main_server_id) DO UPDATE SET
                            last_sync_timestamp = CURRENT_TIMESTAMP,
                            sync_count = sync_count + 1""",
                        (user_id, main_server_id)
                    )

                elif op_type == "update_statistics":
                    trigger_type, success, roles_assigned, user_id = params
                    today = date.today().isoformat()
                    button_inc = 1 if trigger_type == "button" else 0
                    auto_inc = 1 if trigger_type == "auto" else 0
                    manual_inc = 1 if trigger_type == "manual" else 0
                    success_inc = 1 if success else 0
                    failed_inc = 0 if success else 1
                    await db.execute(
                        """INSERT INTO statistics (
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
                            total_roles_assigned = total_roles_assigned + ?""",
                        (today, button_inc, auto_inc, manual_inc, success_inc, failed_inc, roles_assigned,
                         button_inc, auto_inc, manual_inc, success_inc, failed_inc, roles_assigned)
                    )

                elif op_type == "record_sync_session":
                    user_id, trigger_type, success, roles_added, roles_removed, roles_failed, source_servers, errors = params
                    await db.execute(
                        """INSERT INTO sync_sessions (
                            user_id, trigger_type, success,
                            roles_added, roles_removed, roles_failed,
                            source_servers, errors
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (user_id, trigger_type, success,
                         json.dumps(roles_added), json.dumps(roles_removed),
                         json.dumps(roles_failed), json.dumps(source_servers), json.dumps(errors))
                    )

        await db.commit()

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
        action_type: Optional[str] = None,
        days: Optional[int] = None
    ) -> List[Dict]:
        """
        Получить недавние логи

        Args:
            limit: Максимальное количество записей
            user_id: Фильтр по ID пользователя
            action_type: Фильтр по типу действия
            days: Фильтр по количеству дней (None = без ограничения)

        Returns:
            Список логов
        """
        query = "SELECT * FROM sync_logs WHERE 1=1"
        params = []

        if days is not None:
            query += " AND timestamp >= datetime('now', ?)"
            params.append(f'-{days} days')

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
        result = dict(row) if row else {}

        # unique_users_synced нельзя корректно инкрементировать в statistics
        # (upsert не знает, был ли пользователь уже учтён сегодня), поэтому
        # считаем уникальных пользователей на чтении из sync_sessions —
        # там одна запись на каждую синхронизацию.
        unique_query = """
        SELECT COUNT(DISTINCT user_id) AS unique_users_synced
        FROM sync_sessions
        WHERE timestamp >= datetime('now', ?)
        """
        unique_row = await self._fetchone(unique_query, (f'-{days} days',))
        result['unique_users_synced'] = (
            unique_row['unique_users_synced'] if unique_row else 0
        )

        return result

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

    # ============ ObjMapper: авторизация скрипта ============

    async def create_objmapper_token(self, user_id: str, token: str, expires_at: str) -> None:
        """Сохранить 6-значный токен привязки ObjMapper"""
        query = """
        INSERT INTO objmapper_link_tokens (discord_user_id, token, expires_at, is_used)
        VALUES (?, ?, ?, 0)
        """
        await self._execute(query, (str(user_id), token, expires_at))

    async def get_objmapper_token(self, token: str) -> Optional[Dict]:
        """Получить запись токена привязки по значению"""
        query = "SELECT * FROM objmapper_link_tokens WHERE token = ?"
        row = await self._fetchone(query, (token,))
        return dict(row) if row else None

    async def mark_objmapper_token_used(self, token: str) -> None:
        """Пометить токен привязки как использованный (одноразовый)"""
        query = "UPDATE objmapper_link_tokens SET is_used = 1 WHERE token = ?"
        await self._execute(query, (token,))

    async def upsert_objmapper_link(self, user_id: str, nick: str, auth_token: str) -> None:
        """
        Создать/обновить привязку Discord-аккаунт ↔ SAMP-ник.

        Ник уникален: повторная привязка того же ника перезаписывает auth_token
        и владельца. Старый auth_token того же владельца удаляется, чтобы у одного
        Discord-аккаунта не плодились параллельные токены на разные ники.
        """
        async with self._write_lock:
            db = await self._get_connection()
            # Убираем прежние привязки этого Discord-аккаунта (один аккаунт — один ник)
            await db.execute(
                "DELETE FROM objmapper_auth_links WHERE discord_user_id = ?",
                (str(user_id),),
            )
            await db.execute(
                """
                INSERT INTO objmapper_auth_links
                    (discord_user_id, samp_nick, auth_token, last_seen_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(samp_nick) DO UPDATE SET
                    discord_user_id = excluded.discord_user_id,
                    auth_token = excluded.auth_token,
                    last_seen_at = CURRENT_TIMESTAMP
                """,
                (str(user_id), nick, auth_token),
            )
            await db.commit()

    async def get_objmapper_link_by_token(self, auth_token: str) -> Optional[Dict]:
        """Получить привязку по постоянному auth_token (Bearer)"""
        query = "SELECT * FROM objmapper_auth_links WHERE auth_token = ?"
        row = await self._fetchone(query, (auth_token,))
        return dict(row) if row else None

    async def touch_objmapper_link(self, auth_token: str, script_version: Optional[str]) -> None:
        """Обновить last_seen_at и версию скрипта при успешной валидации"""
        query = """
        UPDATE objmapper_auth_links
        SET last_seen_at = CURRENT_TIMESTAMP, script_version = ?
        WHERE auth_token = ?
        """
        await self._execute(query, (script_version, auth_token))

    # ============ Заявки на получение ролей ============

    async def create_request(
        self,
        message_id: int,
        user_id: int,
        embed: dict,
    ) -> None:
        """
        Создать заявку на получение ролей.

        Args:
            message_id: ID сообщения с заявкой в админ-канале (первичный ключ)
            user_id: ID пользователя, подавшего заявку
            embed: Словарь embed (Embed.to_dict()), сериализуется в JSON
        """
        query = """
        INSERT INTO requests (message_id, user_id, embed, status, created_at)
        VALUES (?, ?, ?, 'pending', CURRENT_TIMESTAMP)
        """
        await self._execute(query, (message_id, user_id, json.dumps(embed)))
        logger.debug(f"Создана заявка {message_id} от пользователя {user_id}")

    async def set_request_approved(self, message_id: int, finished_by: int) -> None:
        """Пометить заявку как одобренную"""
        query = """
        UPDATE requests
        SET status = 'approved', finished_by = ?, finished_at = CURRENT_TIMESTAMP
        WHERE message_id = ?
        """
        await self._execute(query, (finished_by, message_id))
        logger.debug(f"Заявка {message_id} одобрена пользователем {finished_by}")

    async def set_request_rejected(
        self,
        message_id: int,
        finished_by: int,
        reject_reason: str,
    ) -> None:
        """Пометить заявку как отклонённую с указанием причины"""
        query = """
        UPDATE requests
        SET status = 'rejected', finished_by = ?, finished_at = CURRENT_TIMESTAMP,
            reject_reason = ?
        WHERE message_id = ?
        """
        await self._execute(query, (finished_by, reject_reason, message_id))
        logger.debug(f"Заявка {message_id} отклонена пользователем {finished_by}")

    async def get_pending_requests(self) -> List[Dict]:
        """
        Получить все заявки в статусе pending (для перевешивания view на on_ready).

        Returns:
            Список заявок с распарсенным полем embed
        """
        query = "SELECT * FROM requests WHERE status = 'pending' ORDER BY created_at"
        rows = await self._fetchall(query)
        return [self._parse_request_row(row) for row in rows]

    async def get_requests_by_user(self, user_id: int, limit: int = 50) -> List[Dict]:
        """
        Получить историю заявок пользователя (для /search).

        Args:
            user_id: ID пользователя
            limit: Максимальное количество записей

        Returns:
            Список заявок с распарсенным полем embed
        """
        query = """
        SELECT * FROM requests
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """
        rows = await self._fetchall(query, (user_id, limit))
        return [self._parse_request_row(row) for row in rows]

    @staticmethod
    def _parse_request_row(row) -> Dict:
        """Преобразовать строку заявки в словарь с распарсенным embed"""
        request = dict(row)
        try:
            request['embed'] = json.loads(request['embed'])
        except (json.JSONDecodeError, TypeError):
            request['embed'] = {}
        return request
