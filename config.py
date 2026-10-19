"""Пути приложения, загрузка/сохранение конфигурации и профилей."""
import json
import logging
import os
import sys
import tempfile
import time
from logging.handlers import RotatingFileHandler

import platformdirs

from enums import UpdateChannel

APP_NAME = "MinecraftModUpdater"

CONFIG_DIR = platformdirs.user_data_dir(APP_NAME, appauthor=False)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
CACHE_DIR = os.path.join(CONFIG_DIR, "cache")
CACHE_FILE = os.path.join(CACHE_DIR, "mod_cache.db")
ICON_CACHE_DIR = os.path.join(CACHE_DIR, "icons96")
APP_VERSION = "3.0.0-beta"
# Репозиторий на GitHub для «Сообщить об ошибке» (owner/repo). Пусто — кнопка не показывается.
GITHUB_REPOSITORY = ""
CURRENT_LOG: str = ""  # Путь к логу текущего запуска (для отчёта о сбое)
LOG_DIR = os.path.join(CONFIG_DIR, "logs")

DEFAULT_GAME_VERSION = "1.21.11"
DEFAULT_LOADER = "fabric"
DEFAULT_CHANNEL = UpdateChannel.BETA
LOG_RETENTION_DAYS = 14


def resource_path(relative: str) -> str:
    """Путь к файлу ресурса: рядом с исходниками или внутри сборки PyInstaller."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


APP_ICON = "icon.ico"


def default_profile() -> dict:
    return {
        "mods_path": "",
        "game_version": DEFAULT_GAME_VERSION,
        "loader": DEFAULT_LOADER,
        "manual_mode": False,
        "channel": DEFAULT_CHANNEL.value,
        "source": "manual",
        "loader_version": "",
        "pinned": {},  # Закреплённые версии: ключ мода (project_id или имя файла) -> версия
    }


def default_settings() -> dict:
    return {
        "auto_check": True,          # Проверять обновления автоматически при открытии сборки
        "discover_launchers": True,  # Искать сборки Prism/Modrinth/CurseForge при запуске
        "parallel_downloads": 3,
        "view_mode": "flat",           # Вид списка: flat — все моды, mine — только мои, tree — дерево зависимостей
        "expand_dependencies": False,  # В дереве зависимости раскрыты сразу
        "compact_rows": False,         # Строки в одну линию
        "details_on_select": True,     # Панель мода открывается при выборе строки
        "group_by_status": True,       # Заголовки групп («Обновления», «Актуальные»…) при сортировке по важности
        "changelog_before_update": True,  # Перед «Обновить все» показывать список изменений
        "auto_snapshot": True,         # Снимок сборки перед массовыми изменениями
        "snapshots_keep": 10,          # Сколько автоматических снимков хранить
        "animations": True,
        # Оформление
        "accent": "green",             # Ключ из theme.ACCENTS или цвет #rrggbb
        "ui_scale": 100,               # Масштаб интерфейса, % (применяется после перезапуска)
        "columns": ["description", "versions"],  # Что показывать в строке мода (см. ui.mod_list.COLUMNS)
        "minimal_mode": False,         # Режим «Минимум»: только список и кнопка «Обновить»
        "per_profile_view": True,      # Запоминать вид списка и сортировку для каждой сборки
        "shortcuts": {},               # Переназначенные горячие клавиши: действие -> сочетание
        # Бэкапы
        "backup_max_days": 30,         # Удалять бэкапы операций старше (0 — не удалять)
        "backup_max_mb": 1024,         # Ограничение места под бэкапы одной папки (0 — без ограничения)
        "abandoned_warning": True,     # Предупреждать о модах, которые давно не обновлялись
    }


def default_config() -> dict:
    return {"theme": "system", "current_profile": "Default", "profiles": {"Default": default_profile()},
            "settings": default_settings(), "hidden_instances": [], "window": {}, "mod_tags": {},
            "mod_notes": {}, "favorites": [], "last_seen": {}}


def _normalize(config: dict) -> dict:
    """Приводит конфиг к актуальному формату (миграция старого формата без профилей)."""
    if "profiles" not in config or not isinstance(config.get("profiles"), dict) or not config["profiles"]:
        legacy = default_profile()
        for key in ("mods_path", "game_version", "loader", "manual_mode"):
            if key in config:
                legacy[key] = config[key]
        config = {"theme": config.get("theme", "system"), "current_profile": "Default", "profiles": {"Default": legacy}}

    for name, prof in config["profiles"].items():
        merged = default_profile()
        merged.update(prof or {})
        config["profiles"][name] = merged

    if config.get("current_profile") not in config["profiles"]:
        config["current_profile"] = next(iter(config["profiles"]))
    config.setdefault("theme", "system")
    settings = default_settings()
    settings.update(config.get("settings") or {})
    config["settings"] = settings
    if not isinstance(config.get("hidden_instances"), list):
        config["hidden_instances"] = []
    if not isinstance(config.get("window"), dict):
        config["window"] = {}
    if not isinstance(config.get("mod_tags"), dict):  # Метки пользователя: ключ мода -> [метки]
        config["mod_tags"] = {}
    if not isinstance(config.get("mod_notes"), dict):  # Заметки: ключ мода -> текст
        config["mod_notes"] = {}
    if not isinstance(config.get("favorites"), list):  # Избранные моды: ключи
        config["favorites"] = []
    if not isinstance(config.get("last_seen"), dict):  # Сборка -> {проект: версия} на момент прошлой проверки
        config["last_seen"] = {}
    return config


def load_config(path: str = CONFIG_FILE) -> dict:
    """Загружает конфиг. При отсутствии или повреждении файла возвращает конфиг по умолчанию."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
        logging.info("Конфигурация успешно загружена.")
        return _normalize(config)
    except FileNotFoundError:
        logging.info("Файл конфигурации не найден, используются значения по умолчанию.")
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError, TypeError) as e:
        logging.warning(f"Ошибка при чтении файла конфигурации ({e}), будут использованы значения по умолчанию.")
    return _normalize(default_config())


def save_config(config: dict, path: str = CONFIG_FILE) -> None:
    """Атомарно сохраняет конфиг: запись во временный файл и замена, чтобы не повредить его при сбое."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".config_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def default_minecraft_dir() -> str:
    """Стандартное расположение .minecraft для текущей ОС."""
    if os.name == "nt":
        return os.path.join(os.getenv("APPDATA") or os.path.expanduser("~"), ".minecraft")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/minecraft")
    return os.path.expanduser("~/.minecraft")


def cleanup_old_logs(log_dir: str = LOG_DIR, max_age_days: int = LOG_RETENTION_DAYS) -> None:
    """Удаляет файлы логов старше max_age_days."""
    if not os.path.isdir(log_dir):
        return
    threshold = time.time() - max_age_days * 86400
    for name in os.listdir(log_dir):
        path = os.path.join(log_dir, name)
        try:
            if name.endswith(".log") and os.path.isfile(path) and os.path.getmtime(path) < threshold:
                os.remove(path)
        except OSError:
            pass


def setup_file_logging(log_name: str, level=logging.DEBUG) -> str:
    """Настраивает файловое логирование с ротацией и очисткой старых логов. Возвращает путь к логу."""
    os.makedirs(LOG_DIR, exist_ok=True)
    cleanup_old_logs()
    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    log_path = os.path.join(LOG_DIR, f"{log_name}_{timestamp}.log")
    handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    # urllib3 слишком подробно пишет в DEBUG
    logging.getLogger("urllib3").setLevel(logging.INFO)
    global CURRENT_LOG
    CURRENT_LOG = log_path
    return log_path


# --- Перенос настроек на другой компьютер ---

EXPORT_FORMAT = "minecraft-mod-updater-settings"


def export_config(config: dict, path: str) -> None:
    """Сохраняет сборки, настройки, метки, заметки, избранное и закрепления в один JSON-файл."""
    data = {"format": EXPORT_FORMAT, "app_version": APP_VERSION, **{k: v for k, v in config.items() if k != "window"}}
    save_config(data, path)


def import_config(path: str) -> dict:
    """Читает файл экспорта. ValueError — если это не файл настроек программы."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"Не удалось прочитать файл: {e}") from e
    if not isinstance(data, dict) or data.get("format") != EXPORT_FORMAT:
        raise ValueError("Это не файл настроек Minecraft Mod Updater.")
    data.pop("format", None)
    data.pop("app_version", None)
    return _normalize(data)
