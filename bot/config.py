"""
Система загрузки и валидации конфигурации
"""

import yaml
import json
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
from bot.utils.logger import get_logger
from bot.utils.errors import ConfigurationError

logger = get_logger("config")


# Допустимые источники истины для маппинга
SOURCE_OF_TRUTH_VALUES = ("fraction_discord", "forum", "manual")
DEFAULT_SOURCE_OF_TRUTH = "fraction_discord"


@dataclass
class RoleMapping:
    """Маппинг роли между серверами"""
    id: str
    source_server_id: int
    source_role_id: int
    target_server_id: int
    target_role_id: int
    description: str
    enabled: bool = True
    # Источник истины для этого маппинга:
    #   fraction_discord — правда = роль на фракционном Discord (автосинк);
    #   forum            — правда = ранг на форуме фракции (проверка через ForumProvider);
    #   manual           — без автопроверки, только ручное одобрение заявки.
    source_of_truth: str = DEFAULT_SOURCE_OF_TRUTH
    # Имя ранга/группы на форуме (используется при source_of_truth == 'forum')
    forum_rank: str = ''

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'RoleMapping':
        """Создать объект из словаря"""
        sot = data.get('source_of_truth', DEFAULT_SOURCE_OF_TRUTH)
        if sot not in SOURCE_OF_TRUTH_VALUES:
            logger.warning(
                f"Некорректный source_of_truth '{sot}' в маппинге {data.get('id')}, "
                f"использую '{DEFAULT_SOURCE_OF_TRUTH}'"
            )
            sot = DEFAULT_SOURCE_OF_TRUTH
        return cls(
            id=data['id'],
            source_server_id=int(data['source_server_id']),
            source_role_id=int(data['source_role_id']),
            target_server_id=int(data['target_server_id']),
            target_role_id=int(data['target_role_id']),
            description=data.get('description', ''),
            enabled=data.get('enabled', True),
            source_of_truth=sot,
            forum_rank=data.get('forum_rank', '')
        )

    def to_dict(self) -> Dict[str, Any]:
        """Конвертировать в словарь"""
        return {
            'id': self.id,
            'source_server_id': str(self.source_server_id),
            'source_role_id': str(self.source_role_id),
            'target_server_id': str(self.target_server_id),
            'target_role_id': str(self.target_role_id),
            'description': self.description,
            'enabled': self.enabled,
            'source_of_truth': self.source_of_truth,
            'forum_rank': self.forum_rank
        }


class Config:
    """Класс для управления конфигурацией бота"""

    def __init__(self, config_path: str = "config/config.yaml", mappings_path: str = "config/role_mappings.json"):
        """
        Инициализация конфигурации

        Args:
            config_path: Путь к YAML файлу конфигурации
            mappings_path: Путь к JSON файлу с маппингами ролей
        """
        self.config_path = Path(config_path)
        self.mappings_path = Path(mappings_path)
        self._config: Dict[str, Any] = {}
        self._role_mappings: List[RoleMapping] = []

        self._load_config()
        self._load_mappings()
        self._validate()

    def _load_config(self) -> None:
        """Загрузить основную конфигурацию из YAML"""
        try:
            if not self.config_path.exists():
                raise ConfigurationError(f"Файл конфигурации не найден: {self.config_path}")

            with open(self.config_path, 'r', encoding='utf-8') as f:
                self._config = yaml.safe_load(f)

            logger.info(f"Конфигурация загружена из {self.config_path}")

        except yaml.YAMLError as e:
            raise ConfigurationError(f"Ошибка парсинга YAML: {e}")
        except Exception as e:
            raise ConfigurationError(f"Ошибка загрузки конфигурации: {e}")

    def _load_mappings(self) -> None:
        """Загрузить маппинги ролей из JSON"""
        try:
            if not self.mappings_path.exists():
                logger.warning(f"Файл маппингов не найден: {self.mappings_path}. Создаю пустой файл.")
                self._create_default_mappings_file()
                return

            with open(self.mappings_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            self._role_mappings = [
                RoleMapping.from_dict(mapping)
                for mapping in data.get('mappings', [])
            ]

            logger.info(f"Загружено {len(self._role_mappings)} маппингов ролей")

        except json.JSONDecodeError as e:
            raise ConfigurationError(f"Ошибка парсинга JSON: {e}")
        except Exception as e:
            raise ConfigurationError(f"Ошибка загрузки маппингов: {e}")

    def _create_default_mappings_file(self) -> None:
        """Создать файл маппингов с примером"""
        default_data = {
            "mappings": [
                {
                    "id": "example_mapping",
                    "source_server_id": "000000000000000000",
                    "source_role_id": "111111111111111111",
                    "target_server_id": "222222222222222222",
                    "target_role_id": "333333333333333333",
                    "description": "Пример маппинга роли",
                    "enabled": False
                }
            ]
        }

        self.mappings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.mappings_path, 'w', encoding='utf-8') as f:
            json.dump(default_data, f, indent=2, ensure_ascii=False)

        logger.info(f"Создан файл маппингов по умолчанию: {self.mappings_path}")

    def _validate(self) -> None:
        """Валидация конфигурации"""
        required_keys = ['bot', 'sync', 'database', 'logging']
        for key in required_keys:
            if key not in self._config:
                raise ConfigurationError(f"Отсутствует обязательная секция в конфигурации: {key}")

        # Проверяем основные параметры бота
        bot_config = self._config['bot']
        if 'main_server_id' not in bot_config:
            raise ConfigurationError("Отсутствует bot.main_server_id в конфигурации")

        logger.info("Конфигурация валидна")

    def reload_mappings(self) -> None:
        """Перезагрузить маппинги ролей"""
        logger.info("Перезагрузка маппингов ролей...")
        self._load_mappings()

    def save_mappings(self) -> None:
        """Сохранить маппинги ролей в файл"""
        try:
            data = {
                "mappings": [mapping.to_dict() for mapping in self._role_mappings]
            }

            with open(self.mappings_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            logger.info(f"Маппинги сохранены в {self.mappings_path}")

        except Exception as e:
            logger.error(f"Ошибка сохранения маппингов: {e}")
            raise ConfigurationError(f"Не удалось сохранить маппинги: {e}")

    # ============ Геттеры для конфигурации ============

    def get_main_server_id(self) -> int:
        """Получить ID главного сервера"""
        return int(self._config['bot']['main_server_id'])

    def get_command_prefix(self) -> str:
        """Получить префикс команд"""
        return self._config['bot'].get('command_prefix', '!')

    def get_sync_button_channel_id(self) -> Optional[int]:
        """Получить ID канала для кнопки синхронизации"""
        channel_id = self._config['bot'].get('sync_button_channel_id')
        return int(channel_id) if channel_id else None

    def get_admin_role_ids(self) -> List[int]:
        """Получить список ID ролей администраторов"""
        role_ids = self._config['bot'].get('admin_role_ids', [])
        return [int(rid) for rid in role_ids]

    def get_log_channel_id(self) -> Optional[int]:
        """Получить ID канала для логирования"""
        channel_id = self._config['bot'].get('log_channel_id')
        return int(channel_id) if channel_id else None

    def get_fraction_server_ids(self) -> List[int]:
        """
        Явно сконфигурированные ID фракционных серверов.

        Если список пуст — потребители (напр. cog membership) трактуют все
        гильдии бота, кроме главного сервера, как фракционные.
        """
        raw = self._config['bot'].get('fraction_server_ids', []) or []
        result = []
        for sid in raw:
            if sid is None or str(sid).strip() == '':
                continue
            try:
                result.append(int(sid))
            except (TypeError, ValueError):
                logger.warning(f"Некорректный ID фракционного сервера в конфиге: {sid!r}")
        return result

    # ============ Геттеры для заявок на роли ============

    def _requests_section(self) -> Dict[str, Any]:
        """Секция requests (может отсутствовать)"""
        return self._config.get('requests', {}) or {}

    def is_requests_enabled(self) -> bool:
        """Включён ли flow заявок на роли"""
        return bool(self._requests_section().get('enabled', False))

    def get_request_button_channel_id(self) -> Optional[int]:
        """ID канала, где размещается кнопка 'Получить роли' (аналог CL_REQUEST_CH)"""
        channel_id = self._requests_section().get('button_channel_id')
        return int(channel_id) if channel_id else None

    def get_request_admin_channel_id(self) -> Optional[int]:
        """ID канала админ-ревью заявок (аналог ADM_ROLES_CH)"""
        channel_id = self._requests_section().get('admin_channel_id')
        return int(channel_id) if channel_id else None

    def get_request_admin_role_ids(self) -> List[int]:
        """ID ролей, которые могут одобрять/отклонять заявки"""
        raw = self._requests_section().get('admin_role_ids', []) or []
        result = []
        for rid in raw:
            if rid is None or str(rid).strip() == '':
                continue
            try:
                result.append(int(rid))
            except (TypeError, ValueError):
                logger.warning(f"Некорректный ID роли админа заявок в конфиге: {rid!r}")
        return result

    def is_auto_sync_enabled(self) -> bool:
        """Проверить включена ли автоматическая синхронизация"""
        return self._config['sync'].get('auto_sync_enabled', True)

    def get_sync_interval(self) -> int:
        """Получить интервал синхронизации в секундах"""
        return self._config['sync'].get('sync_interval_seconds', 300)

    def is_batch_sync_enabled(self) -> bool:
        """Проверить включена ли массовая синхронизация"""
        return self._config['sync'].get('batch_sync_enabled', True)

    def get_database_path(self) -> str:
        """Получить путь к базе данных"""
        return self._config['database'].get('path', 'data/bot.db')

    # ============ Геттеры для ObjMapper API ============

    def _objmapper_section(self) -> Dict[str, Any]:
        """Секция objmapper (может отсутствовать)"""
        return self._config.get('objmapper', {}) or {}

    def is_objmapper_enabled(self) -> bool:
        """Включён ли API авторизации ObjMapper"""
        return bool(self._objmapper_section().get('enabled', False))

    def get_objmapper_api_host(self) -> str:
        """Хост HTTP API ObjMapper"""
        return self._objmapper_section().get('api_host', '0.0.0.0')

    def get_objmapper_api_port(self) -> int:
        """Порт HTTP API ObjMapper"""
        return int(self._objmapper_section().get('api_port', 3002))

    def get_objmapper_token_ttl(self) -> int:
        """TTL токена привязки ObjMapper в секундах"""
        return int(self._objmapper_section().get('token_ttl_seconds', 3600))

    def get_objmapper_nick_limits(self) -> tuple:
        """(min, max) длина SAMP-ника"""
        sec = self._objmapper_section()
        return int(sec.get('nick_min_length', 1)), int(sec.get('nick_max_length', 24))

    def is_objmapper_telemetry_enabled(self) -> bool:
        """Включён ли приём телеметрии использования (по умолчанию да)"""
        return bool(self._objmapper_section().get('telemetry_enabled', True))

    def get_objmapper_allowed_role_ids(self) -> List[int]:
        """ID ролей, дающих доступ к ObjMapper (пустые/плейсхолдеры отброшены)"""
        raw = self._objmapper_section().get('allowed_role_ids', []) or []
        result = []
        for rid in raw:
            if rid is None or str(rid).strip() == '':
                continue
            try:
                result.append(int(rid))
            except (TypeError, ValueError):
                logger.warning(f"Некорректный ID роли ObjMapper в конфиге: {rid!r}")
        return result

    # ============ Геттеры для форума (источник истины) ============

    def _forum_section(self) -> Dict[str, Any]:
        """Секция forum (может отсутствовать)"""
        return self._config.get('forum', {}) or {}

    def is_forum_enabled(self) -> bool:
        """Включена ли интеграция с форумом (источник истины 'forum')"""
        return bool(self._forum_section().get('enabled', False))

    def get_forum_provider_type(self) -> str:
        """Тип провайдера форума: 'http' (реальный форум) или 'stub' (заглушка)"""
        return self._forum_section().get('provider', 'stub')

    def get_forum_base_url(self) -> str:
        """Базовый URL фракционного форума"""
        return self._forum_section().get('base_url', '')

    def get_forum_request_timeout(self) -> int:
        """Таймаут HTTP-запросов к форуму в секундах"""
        return int(self._forum_section().get('request_timeout_seconds', 10))

    def get_forum_cache_ttl(self) -> int:
        """TTL кэша ответов форума в секундах (чтобы не дёргать форум на каждый синк)"""
        return int(self._forum_section().get('cache_ttl_seconds', 300))

    def get_log_level(self) -> str:
        """Получить уровень логирования"""
        return self._config['logging'].get('level', 'INFO')

    def get_log_file_path(self) -> str:
        """Получить путь к файлу логов"""
        return self._config['logging'].get('file_path', 'logs/bot.log')

    def get_log_max_bytes(self) -> int:
        """Получить максимальный размер файла логов"""
        return self._config['logging'].get('max_bytes', 10485760)

    def get_log_backup_count(self) -> int:
        """Получить количество резервных копий логов"""
        return self._config['logging'].get('backup_count', 5)

    # ============ Геттеры для маппингов ролей ============

    def get_role_mappings(self) -> List[RoleMapping]:
        """Получить все маппинги ролей"""
        return [m for m in self._role_mappings if m.enabled]

    def get_all_role_mappings(self) -> List[RoleMapping]:
        """Получить все маппинги ролей (включая отключенные)"""
        return self._role_mappings

    def get_mapping_by_id(self, mapping_id: str) -> Optional[RoleMapping]:
        """Получить маппинг по ID"""
        for mapping in self._role_mappings:
            if mapping.id == mapping_id:
                return mapping
        return None

    def add_role_mapping(self, mapping: RoleMapping) -> None:
        """
        Добавить новый маппинг роли

        Args:
            mapping: Объект маппинга
        """
        # Проверяем, не существует ли уже такой ID
        if any(m.id == mapping.id for m in self._role_mappings):
            raise ConfigurationError(f"Маппинг с ID {mapping.id} уже существует")

        self._role_mappings.append(mapping)
        self.save_mappings()
        logger.info(f"Добавлен новый маппинг: {mapping.id}")

    def remove_role_mapping(self, mapping_id: str) -> bool:
        """
        Удалить маппинг роли

        Args:
            mapping_id: ID маппинга

        Returns:
            True если удален успешно
        """
        original_len = len(self._role_mappings)
        self._role_mappings = [m for m in self._role_mappings if m.id != mapping_id]

        if len(self._role_mappings) < original_len:
            self.save_mappings()
            logger.info(f"Удален маппинг: {mapping_id}")
            return True

        return False

    def update_role_mapping(self, mapping: RoleMapping) -> bool:
        """
        Обновить существующий маппинг

        Args:
            mapping: Обновленный объект маппинга

        Returns:
            True если обновлен успешно
        """
        for i, m in enumerate(self._role_mappings):
            if m.id == mapping.id:
                self._role_mappings[i] = mapping
                self.save_mappings()
                logger.info(f"Обновлен маппинг: {mapping.id}")
                return True

        return False
