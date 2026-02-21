"""
Схема базы данных для Discord Role Sync Bot
"""

import aiosqlite
from pathlib import Path
from typing import Optional
from bot.utils.logger import get_logger

logger = get_logger("database.models")


# SQL запросы для создания таблиц

CREATE_SYNC_STATE_TABLE = """
CREATE TABLE IF NOT EXISTS sync_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    main_server_id INTEGER NOT NULL,
    last_sync_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sync_count INTEGER DEFAULT 1,
    UNIQUE(user_id, main_server_id)
);
"""

CREATE_ROLE_ASSIGNMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS role_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    source_server_id INTEGER NOT NULL,
    source_role_id INTEGER NOT NULL,
    target_server_id INTEGER NOT NULL,
    target_role_id INTEGER NOT NULL,
    assigned_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    assignment_type TEXT NOT NULL CHECK(assignment_type IN ('button', 'auto', 'manual'))
);
"""

CREATE_ROLE_ASSIGNMENTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_user_target
ON role_assignments(user_id, target_server_id);
"""

CREATE_SYNC_LOGS_TABLE = """
CREATE TABLE IF NOT EXISTS sync_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    user_id INTEGER NOT NULL,
    action_type TEXT NOT NULL CHECK(action_type IN (
        'sync_requested', 'role_added', 'role_removed', 'sync_failed', 'sync_success'
    )),
    source_server_id INTEGER,
    source_role_id INTEGER,
    target_server_id INTEGER,
    target_role_id INTEGER,
    trigger_type TEXT NOT NULL CHECK(trigger_type IN ('button', 'auto', 'manual', 'command')),
    success BOOLEAN NOT NULL DEFAULT 1,
    error_message TEXT
);
"""

CREATE_SYNC_LOGS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_timestamp ON sync_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_user ON sync_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_action_type ON sync_logs(action_type);
"""

CREATE_STATISTICS_TABLE = """
CREATE TABLE IF NOT EXISTS statistics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stat_date DATE NOT NULL UNIQUE,
    total_syncs INTEGER DEFAULT 0,
    button_syncs INTEGER DEFAULT 0,
    auto_syncs INTEGER DEFAULT 0,
    manual_syncs INTEGER DEFAULT 0,
    successful_syncs INTEGER DEFAULT 0,
    failed_syncs INTEGER DEFAULT 0,
    unique_users_synced INTEGER DEFAULT 0,
    total_roles_assigned INTEGER DEFAULT 0
);
"""

CREATE_SYNC_SESSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS sync_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    user_id INTEGER NOT NULL,
    trigger_type TEXT NOT NULL,
    success BOOLEAN NOT NULL DEFAULT 1,
    roles_added TEXT DEFAULT '[]',
    roles_removed TEXT DEFAULT '[]',
    roles_failed TEXT DEFAULT '[]',
    source_servers TEXT DEFAULT '[]',
    errors TEXT DEFAULT '[]'
);
"""

CREATE_SYNC_SESSIONS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_sessions_timestamp ON sync_sessions(timestamp);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sync_sessions(user_id);
"""

CREATE_ROLE_MAPPING_CACHE_TABLE = """
CREATE TABLE IF NOT EXISTS role_mapping_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mapping_id TEXT NOT NULL UNIQUE,
    source_server_id INTEGER NOT NULL,
    source_role_id INTEGER NOT NULL,
    target_server_id INTEGER NOT NULL,
    target_role_id INTEGER NOT NULL,
    enabled BOOLEAN DEFAULT 1,
    description TEXT,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_ROLE_MAPPING_CACHE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_source
ON role_mapping_cache(source_server_id, source_role_id);
"""


async def initialize_database(db_path: str) -> None:
    """
    Инициализация базы данных - создание всех таблиц и индексов

    Args:
        db_path: Путь к файлу базы данных
    """
    logger.info(f"Инициализация базы данных: {db_path}")

    # Создаем директорию если не существует
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    try:
        async with aiosqlite.connect(db_path) as db:
            # Создаем таблицы
            await db.execute(CREATE_SYNC_STATE_TABLE)
            logger.debug("Таблица sync_state создана")

            await db.execute(CREATE_ROLE_ASSIGNMENTS_TABLE)
            await db.execute(CREATE_ROLE_ASSIGNMENTS_INDEX)
            logger.debug("Таблица role_assignments создана")

            await db.execute(CREATE_SYNC_LOGS_TABLE)
            # Создаем индексы для sync_logs
            for index_sql in CREATE_SYNC_LOGS_INDEXES.split(';'):
                if index_sql.strip():
                    await db.execute(index_sql)
            logger.debug("Таблица sync_logs создана")

            await db.execute(CREATE_STATISTICS_TABLE)
            logger.debug("Таблица statistics создана")

            await db.execute(CREATE_SYNC_SESSIONS_TABLE)
            for index_sql in CREATE_SYNC_SESSIONS_INDEXES.split(';'):
                if index_sql.strip():
                    await db.execute(index_sql)
            logger.debug("Таблица sync_sessions создана")

            await db.execute(CREATE_ROLE_MAPPING_CACHE_TABLE)
            await db.execute(CREATE_ROLE_MAPPING_CACHE_INDEX)
            logger.debug("Таблица role_mapping_cache создана")

            await db.commit()
            logger.info("База данных успешно инициализирована")

    except Exception as e:
        logger.error(f"Ошибка инициализации базы данных: {e}", exc_info=True)
        raise


async def get_database_connection(db_path: str) -> aiosqlite.Connection:
    """
    Получить подключение к базе данных

    Args:
        db_path: Путь к файлу базы данных

    Returns:
        Объект подключения к БД
    """
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    return conn
