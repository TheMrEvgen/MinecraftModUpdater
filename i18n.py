"""
Перевод интерфейса. Исходный язык — русский: строки в коде пишутся по-русски и оборачиваются в tr(), поэтому
отдельного ru.json нет. Переводы лежат в locales/<код>.json (ключ — русская строка, «@name» — название языка):
новый язык — это новый файл, программа сама добавит его в настройки. Заготовку делает
tools/i18n_check.py --template <код>; непереведённые строки (пустые) показываются по-русски.

    tr("Удалено: {0}", count)        — перевод + подстановка (позиционно, как str.format)
    plural(n, "мод", "мода", "модов") — склонение по числу для текущего языка

Язык выбирается в настройках и применяется после перезапуска: многие подписи создаются один раз.
Порядок выбора: переменная окружения MODUPDATER_LANG, затем settings.language в конфиге, иначе русский.
"""
import json
import os
import sys
from typing import Dict, Optional

import platformdirs

DEFAULT_LANGUAGE = "ru"
NAME_KEY = "@name"  # Название языка в его файле перевода

_language: Optional[str] = None
_catalog: Dict[str, str] = {}


def _locales_dir() -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))  # Как config.resource_path
    return os.path.join(base, "locales")


def _discover_languages() -> Dict[str, str]:
    """Русский (исходный) и все языки из папки locales: код -> название."""
    languages = {DEFAULT_LANGUAGE: "Русский"}
    try:
        names = sorted(os.listdir(_locales_dir()))
    except OSError:
        names = []
    for name in names:
        code, ext = os.path.splitext(name)
        if ext != ".json" or code == DEFAULT_LANGUAGE:
            continue
        try:
            with open(os.path.join(_locales_dir(), name), "r", encoding="utf-8") as f:
                languages[code] = str(json.load(f).get(NAME_KEY) or code)
        except (OSError, ValueError, AttributeError):
            continue
    return languages


LANGUAGES = _discover_languages()


def _configured_language() -> str:
    env = os.getenv("MODUPDATER_LANG")
    if env in LANGUAGES:
        return env
    # Путь как в config.CONFIG_FILE: config сам импортирует i18n, поэтому здесь его не импортируем
    config_file = os.path.join(platformdirs.user_data_dir("MinecraftModUpdater", appauthor=False), "config.json")
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            language = (json.load(f).get("settings") or {}).get("language")
        return language if language in LANGUAGES else DEFAULT_LANGUAGE
    except (OSError, ValueError, AttributeError):
        return DEFAULT_LANGUAGE


def catalog_path(language: str) -> str:
    return os.path.join(_locales_dir(), f"{language}.json")


def set_language(language: str):
    """Выбирает язык и загружает перевод (вызывается при первом tr или в тестах)."""
    global _language, _catalog
    _language = language if language in LANGUAGES else DEFAULT_LANGUAGE
    _catalog = {}
    if _language != DEFAULT_LANGUAGE:
        try:
            with open(catalog_path(_language), "r", encoding="utf-8") as f:
                _catalog = json.load(f)
        except (OSError, ValueError):
            _catalog = {}


def language() -> str:
    if _language is None:
        set_language(_configured_language())
    return _language


def tr(text: str, *args) -> str:
    template = (_catalog.get(text) or text) if language() != DEFAULT_LANGUAGE else text  # Пусто — не переведено
    return template.format(*args) if args else template


def plural(n: int, one: str, few: str, many: str) -> str:
    """Форма слова для числа n. В английском «few» и «many» — множественное число."""
    if language() == "ru":
        if n % 10 == 1 and n % 100 != 11:
            return one
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return few
        return many
    return tr(one) if n == 1 else tr(many)
