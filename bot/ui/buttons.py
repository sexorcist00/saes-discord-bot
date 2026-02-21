"""
Discord UI компоненты - кнопки
"""

import discord
from typing import Optional
from bot.utils.logger import get_logger

logger = get_logger("ui.buttons")


class SyncRolesButton(discord.ui.Button):
    """Кнопка для синхронизации ролей"""

    def __init__(self):
        super().__init__(
            label="Получить роли",
            style=discord.ButtonStyle.primary,
            custom_id="role_sync_button"
        )

    async def callback(self, interaction: discord.Interaction):
        """
        Обработчик нажатия кнопки
        Этот метод будет вызван в SyncButtonCog
        """
        # Эта функция должна быть переопределена в View
        pass


class SyncRolesView(discord.ui.View):
    """View с кнопкой синхронизации"""

    def __init__(self, bot):
        super().__init__(timeout=None)  # Persistent view
        self.bot = bot
        self.add_item(SyncRolesButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """
        Проверка перед обработкой взаимодействия

        Args:
            interaction: Объект взаимодействия

        Returns:
            True если взаимодействие разрешено
        """
        # Проверяем что пользователь не бот
        if interaction.user.bot:
            await interaction.response.send_message(
                "Боты не могут использовать синхронизацию ролей.",
                ephemeral=True
            )
            return False

        return True


class ConfirmButton(discord.ui.Button):
    """Кнопка подтверждения для диалогов"""

    def __init__(self, label: str = "Подтвердить", style: discord.ButtonStyle = discord.ButtonStyle.success):
        super().__init__(
            label=label,
            style=style,
            custom_id="confirm_button"
        )
        self.value = None

    async def callback(self, interaction: discord.Interaction):
        """Обработчик нажатия кнопки подтверждения"""
        self.value = True
        await interaction.response.defer()
        self.view.stop()


class CancelButton(discord.ui.Button):
    """Кнопка отмены для диалогов"""

    def __init__(self, label: str = "Отмена", style: discord.ButtonStyle = discord.ButtonStyle.secondary):
        super().__init__(
            label=label,
            style=style,
            custom_id="cancel_button"
        )
        self.value = None

    async def callback(self, interaction: discord.Interaction):
        """Обработчик нажатия кнопки отмены"""
        self.value = False
        await interaction.response.defer()
        self.view.stop()


class ConfirmView(discord.ui.View):
    """View с кнопками подтверждения/отмены"""

    def __init__(self, timeout: float = 60.0):
        super().__init__(timeout=timeout)
        self.confirm_button = ConfirmButton()
        self.cancel_button = CancelButton()
        self.add_item(self.confirm_button)
        self.add_item(self.cancel_button)
        self.value = None

    async def on_timeout(self):
        """Обработчик истечения времени"""
        self.value = None
        self.stop()


class PaginationButton(discord.ui.Button):
    """Кнопка для пагинации"""

    def __init__(
        self,
        label: str,
        style: discord.ButtonStyle,
        custom_id: str,
        disabled: bool = False
    ):
        super().__init__(
            label=label,
            style=style,
            custom_id=custom_id,
            disabled=disabled
        )

    async def callback(self, interaction: discord.Interaction):
        """Обработчик нажатия кнопки пагинации"""
        await interaction.response.defer()


class PaginationView(discord.ui.View):
    """View с кнопками пагинации"""

    def __init__(self, pages: list, timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.current_page = 0
        self.message: Optional[discord.Message] = None

        # Кнопки навигации
        self.first_button = PaginationButton("⏮️", discord.ButtonStyle.secondary, "first_page")
        self.prev_button = PaginationButton("◀️", discord.ButtonStyle.secondary, "prev_page")
        self.page_indicator = discord.ui.Button(
            label=f"1/{len(pages)}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
            custom_id="page_indicator"
        )
        self.next_button = PaginationButton("▶️", discord.ButtonStyle.secondary, "next_page")
        self.last_button = PaginationButton("⏭️", discord.ButtonStyle.secondary, "last_page")

        # Добавляем кнопки
        self.add_item(self.first_button)
        self.add_item(self.prev_button)
        self.add_item(self.page_indicator)
        self.add_item(self.next_button)
        self.add_item(self.last_button)

        # Устанавливаем обработчики
        self.first_button.callback = self.first_page
        self.prev_button.callback = self.prev_page
        self.next_button.callback = self.next_page
        self.last_button.callback = self.last_page

        self.update_buttons()

    def update_buttons(self):
        """Обновить состояние кнопок"""
        self.first_button.disabled = self.current_page == 0
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page == len(self.pages) - 1
        self.last_button.disabled = self.current_page == len(self.pages) - 1
        self.page_indicator.label = f"{self.current_page + 1}/{len(self.pages)}"

    async def first_page(self, interaction: discord.Interaction):
        """Перейти на первую страницу"""
        self.current_page = 0
        self.update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    async def prev_page(self, interaction: discord.Interaction):
        """Перейти на предыдущую страницу"""
        self.current_page = max(0, self.current_page - 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    async def next_page(self, interaction: discord.Interaction):
        """Перейти на следующую страницу"""
        self.current_page = min(len(self.pages) - 1, self.current_page + 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    async def last_page(self, interaction: discord.Interaction):
        """Перейти на последнюю страницу"""
        self.current_page = len(self.pages) - 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    async def on_timeout(self):
        """Обработчик истечения времени"""
        for item in self.children:
            item.disabled = True
        # Обновляем сообщение в Discord чтобы кнопки отображались как отключённые
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass
        self.stop()
