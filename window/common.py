"""Общие константы главного окна."""
from enums import ModStatus

LOADER_TITLES = {"fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge", "quilt": "Quilt"}
# Что ставит кнопка «Обновить все»: обновления и недостающие обязательные зависимости
BULK_STATUSES = (ModStatus.UPDATE_AVAILABLE, ModStatus.READY_TO_INSTALL, ModStatus.REQUIRED)
PAGE_MODS, PAGE_CATALOG, PAGE_HISTORY, PAGE_STATS, PAGE_LOG, PAGE_SETTINGS = range(6)
PAGE_DOWNLOADS = 6  # Не в меню слева: открывается индикатором загрузки
CONTENT_ICONS = {"mods": "package", "shaders": "sparkles", "resourcepacks": "image", "datapacks": "code"}
ANIMATION_MS = 170
UNDO_LIMIT = 30
