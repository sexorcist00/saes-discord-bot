"""
Создание embed сообщений для Discord
"""

import discord
from datetime import datetime
from typing import List, Dict, Optional
from bot.core.sync_engine import SyncResult
from bot.utils.logger import get_logger

logger = get_logger("ui.embeds")


# Цвета для разных типов сообщений
COLOR_SUCCESS = 0x2ecc71  # Зеленый
COLOR_ERROR = 0xe74c3c    # Красный
COLOR_WARNING = 0xf39c12  # Оранжевый
COLOR_INFO = 0x3498db     # Синий
COLOR_PRIMARY = 0x5865f2  # Discord blurple


def create_sync_result_embed(
    result: SyncResult,
    guild: discord.Guild,
    user: discord.User
) -> discord.Embed:
    """
    Создать упрощённый embed с результатом синхронизации

    Args:
        result: Объект результата синхронизации
        guild: Объект сервера
        user: Объект пользователя

    Returns:
        Embed с простым сообщением о результате
    """
    # Определяем статус и сообщение
    has_failed = len(result.roles_failed) > 0
    has_added = len(result.roles_added) > 0

    if has_failed and not has_added:
        # Все роли не удалось выдать
        title = "⚠️ Не удалось выдать роли"
        description = "Не удалось выдать роли. Попробуйте ещё раз"
        color = COLOR_WARNING
    elif has_failed and has_added:
        # Часть ролей выдана, часть нет
        roles_text = []
        for role_id in result.roles_added:
            role = guild.get_role(role_id)
            if role:
                roles_text.append(role.mention)

        if roles_text:
            added_str = ', '.join(roles_text)
            description = f"Выданы роли: {added_str}\n\nНе все роли удалось выдать. Попробуйте ещё раз"
        else:
            description = "Часть ролей выдана, но не все. Попробуйте ещё раз"
        title = "⚠️ Выданы не все роли"
        color = COLOR_WARNING
    elif not result.success:
        title = "❌ Ошибка"
        description = "Не удалось получить роли. Попробуйте позже"
        color = COLOR_ERROR
    elif result.total_changes > 0:
        # Были изменения
        if len(result.roles_removed) > 0:
            # Были удаления - не показываем детали
            title = "✅ Готово"
            description = "Ваши роли обновлены"
        else:
            # Только добавления - показываем список ролей
            title = "✅ Готово"

            # Формируем список добавленных ролей
            roles_text = []
            for role_id in result.roles_added:
                role = guild.get_role(role_id)
                if role:
                    roles_text.append(role.mention)

            if roles_text:
                if len(roles_text) == 1:
                    description = f"Вы получили роль: {roles_text[0]}"
                else:
                    description = f"Вы получили роли: {', '.join(roles_text)}"
            else:
                description = "Вы получили роли"
        color = COLOR_SUCCESS
    else:
        # Нет изменений
        if len(result.target_roles_calculated or []) == 0:
            title = "⚠️ Нет ролей"
            description = "У вас нет нужных ролей в Discord сервере вашей фракции"
            color = COLOR_WARNING
        else:
            title = "ℹ️ Актуально"
            description = "Ваши роли актуальны"
            color = COLOR_INFO

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=result.timestamp
    )

    embed.set_author(
        name=user.display_name,
        icon_url=user.display_avatar.url
    )

    return embed


def create_sync_button_embed() -> discord.Embed:
    """
    Создать embed для сообщения с кнопкой синхронизации

    Returns:
        Embed для кнопки
    """
    embed = discord.Embed(
        title="<:_:1455510660388753531> Получение ролей",
        description=(
            "Нажмите кнопку ниже, чтобы получить роли вашей фракции.\n\n"
            "Для получения вы должны иметь роли в основном Discord сервере вашей фракции."
        ),
        color=COLOR_PRIMARY
    )

    return embed


def create_error_embed(error_message: str, title: str = "Ошибка") -> discord.Embed:
    """
    Создать embed с сообщением об ошибке

    Args:
        error_message: Текст ошибки
        title: Заголовок

    Returns:
        Embed с ошибкой
    """
    embed = discord.Embed(
        title=f"❌ {title}",
        description=error_message,
        color=COLOR_ERROR,
        timestamp=datetime.now()
    )

    return embed


def create_success_embed(message: str, title: str = "Успешно") -> discord.Embed:
    """
    Создать embed с сообщением об успехе

    Args:
        message: Текст сообщения
        title: Заголовок

    Returns:
        Embed с успехом
    """
    embed = discord.Embed(
        title=f"✅ {title}",
        description=message,
        color=COLOR_SUCCESS,
        timestamp=datetime.now()
    )

    return embed


def create_info_embed(message: str, title: str = "Информация") -> discord.Embed:
    """
    Создать информационный embed

    Args:
        message: Текст сообщения
        title: Заголовок

    Returns:
        Информационный embed
    """
    embed = discord.Embed(
        title=f"ℹ️ {title}",
        description=message,
        color=COLOR_INFO,
        timestamp=datetime.now()
    )

    return embed


def create_mapping_list_embed(
    mappings: List[Dict],
    page: int = 1,
    per_page: int = 10
) -> discord.Embed:
    """
    Создать embed со списком маппингов ролей

    Args:
        mappings: Список маппингов
        page: Номер страницы
        per_page: Количество на странице

    Returns:
        Embed со списком
    """
    total_pages = (len(mappings) - 1) // per_page + 1
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_mappings = mappings[start_idx:end_idx]

    # Считаем статистику
    enabled_count = sum(1 for m in mappings if m.get('enabled', True))
    disabled_count = len(mappings) - enabled_count
    unique_servers = len(set(m.get('source_server_id') for m in mappings))

    embed = discord.Embed(
        title="📋 Список маппингов ролей",
        description=(
            f"Всего: **{len(mappings)}** | "
            f"Активных: **{enabled_count}** | "
            f"Отключенных: **{disabled_count}** | "
            f"Серверов: **{unique_servers}**"
        ),
        color=COLOR_INFO,
        timestamp=datetime.now()
    )

    for i, mapping in enumerate(page_mappings):
        status = "✅" if mapping.get('enabled', True) else "❌"
        field_value = (
            f"**ID:** `{mapping['mapping_id']}`\n"
            f"**Источник:** Сервер {mapping['source_server_id']}, Роль {mapping['source_role_id']}\n"
            f"**Цель:** Роль {mapping['target_role_id']}\n"
            f"**Статус:** {status}"
        )

        if mapping.get('description'):
            field_value += f"\n**Описание:** {mapping['description']}"

        embed.add_field(
            name=f"Маппинг #{start_idx + i + 1}",
            value=field_value,
            inline=False
        )

    embed.set_footer(text=f"Страница {page}/{total_pages}")

    return embed


def create_stats_embed(stats: Dict) -> discord.Embed:
    """
    Создать embed со статистикой

    Args:
        stats: Словарь со статистикой

    Returns:
        Embed со статистикой
    """
    embed = discord.Embed(
        title="📊 Статистика синхронизации",
        color=COLOR_INFO,
        timestamp=datetime.now()
    )

    # Общая статистика
    total_syncs = stats.get('total_syncs', 0) or 0
    successful_syncs = stats.get('successful_syncs', 0) or 0
    failed_syncs = stats.get('failed_syncs', 0) or 0

    success_rate = (successful_syncs / total_syncs * 100) if total_syncs > 0 else 0

    embed.add_field(
        name="Всего синхронизаций",
        value=f"**{total_syncs}**",
        inline=True
    )

    embed.add_field(
        name="Успешных",
        value=f"**{successful_syncs}** ({success_rate:.1f}%)",
        inline=True
    )

    embed.add_field(
        name="Ошибок",
        value=f"**{failed_syncs}**",
        inline=True
    )

    # По типам
    button_syncs = stats.get('button_syncs', 0) or 0
    auto_syncs = stats.get('auto_syncs', 0) or 0
    manual_syncs = stats.get('manual_syncs', 0) or 0

    embed.add_field(
        name="По кнопке",
        value=f"**{button_syncs}**",
        inline=True
    )

    embed.add_field(
        name="Автоматических",
        value=f"**{auto_syncs}**",
        inline=True
    )

    embed.add_field(
        name="Ручных",
        value=f"**{manual_syncs}**",
        inline=True
    )

    # Роли
    total_roles = stats.get('total_roles_assigned', 0) or 0
    embed.add_field(
        name="Назначено ролей",
        value=f"**{total_roles}**",
        inline=True
    )

    # Уникальные пользователи
    unique_users = stats.get('unique_users_synced', 0) or 0
    embed.add_field(
        name="Уникальных пользователей",
        value=f"**{unique_users}**",
        inline=True
    )

    embed.set_footer(text="Статистика за последние 30 дней")

    return embed


def create_sync_history_page(
    sessions: List[Dict],
    guild: discord.Guild,
    page: int,
    total_pages: int
) -> discord.Embed:
    """
    Создать embed-страницу истории синхронизаций

    Args:
        sessions: Список сессий для этой страницы
        guild: Объект сервера (для resolve ролей)
        page: Номер текущей страницы (1-based)
        total_pages: Общее количество страниц

    Returns:
        Embed с историей синхронизаций
    """
    embed = discord.Embed(
        title="📜 История синхронизаций",
        color=COLOR_INFO,
        timestamp=datetime.now()
    )

    trigger_labels = {
        'button': 'Кнопка',
        'auto': 'Авто',
        'manual': 'Ручная',
        'command': 'Команда'
    }

    for session in sessions:
        # Статус
        status_emoji = "✅" if session['success'] else "❌"

        # Время
        try:
            ts = datetime.fromisoformat(session['timestamp'])
            time_str = f"<t:{int(ts.timestamp())}:R>"
        except (ValueError, TypeError):
            time_str = "???"

        # Триггер
        trigger = trigger_labels.get(session['trigger_type'], session['trigger_type'])

        # Заголовок field
        field_name = f"{status_emoji} <@{session['user_id']}> — {trigger} {time_str}"

        # Тело field
        lines = []

        # Добавленные роли
        roles_added = session.get('roles_added', [])
        if roles_added:
            role_mentions = []
            for role_id in roles_added:
                role = guild.get_role(role_id)
                role_mentions.append(role.mention if role else f"`{role_id}`")
            lines.append(f"➕ {', '.join(role_mentions)}")

        # Удалённые роли
        roles_removed = session.get('roles_removed', [])
        if roles_removed:
            role_mentions = []
            for role_id in roles_removed:
                role = guild.get_role(role_id)
                role_mentions.append(role.mention if role else f"`{role_id}`")
            lines.append(f"➖ {', '.join(role_mentions)}")

        # Неудавшиеся роли
        roles_failed = session.get('roles_failed', [])
        if roles_failed:
            role_mentions = []
            for role_id in roles_failed:
                role = guild.get_role(role_id)
                role_mentions.append(role.mention if role else f"`{role_id}`")
            lines.append(f"⚠️ Не выданы: {', '.join(role_mentions)}")

        # Ошибки
        errors = session.get('errors', [])
        if errors:
            error_text = errors[0][:100]
            if len(errors) > 1:
                error_text += f" (+{len(errors) - 1})"
            lines.append(f"💬 {error_text}")

        # Если ничего не произошло
        if not lines:
            if session['success']:
                lines.append("Без изменений")
            else:
                lines.append("Ошибка синхронизации")

        embed.add_field(
            name=field_name,
            value="\n".join(lines),
            inline=False
        )

    embed.set_footer(text=f"Страница {page}/{total_pages}")

    return embed


def create_processing_embed() -> discord.Embed:
    """
    Создать embed с индикатором обработки

    Returns:
        Embed обработки
    """
    embed = discord.Embed(
        title="⏳ Получение ролей...",
        color=COLOR_WARNING
    )

    return embed


def create_help_embed(prefix: str = "!") -> discord.Embed:
    """
    Создать embed со справкой по командам

    Args:
        prefix: Префикс команд

    Returns:
        Embed со справкой
    """
    embed = discord.Embed(
        title="📖 Справка по командам",
        description="Доступные команды для управления ботом синхронизации ролей",
        color=COLOR_INFO
    )

    # Команды для пользователей
    embed.add_field(
        name="Команды пользователей",
        value=(
            f"`Кнопка синхронизации` - Синхронизировать роли\n"
            f"`{prefix}rolestats user` - Ваша статистика синхронизации"
        ),
        inline=False
    )

    # Команды администраторов
    embed.add_field(
        name="Команды администраторов",
        value=(
            f"`{prefix}roleadmin sync_all` - Синхронизировать всех пользователей\n"
            f"`{prefix}roleadmin sync_user <ID>` - Синхронизировать пользователя\n"
            f"`{prefix}roleadmin list_mappings` - Список маппингов\n"
            f"`{prefix}roleadmin reload_config` - Перезагрузить конфигурацию\n"
            f"`{prefix}roleadmin check_permissions` - Проверить права бота"
        ),
        inline=False
    )

    # Команды статистики
    embed.add_field(
        name="Статистика",
        value=(
            f"`{prefix}rolestats overview` - Общая статистика\n"
            f"`{prefix}rolestats logs [лимит]` - Последние логи"
        ),
        inline=False
    )

    embed.set_footer(text=f"Префикс команд: {prefix}")

    return embed
