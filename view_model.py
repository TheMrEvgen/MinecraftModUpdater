"""
Подготовка данных сканирования для списка модов (без Qt).

Сканер возвращает дерево «мод -> зависимости». В интерфейсе показывается плоский список:
все установленные моды (включая установленные зависимости) и недостающие обязательные
зависимости. Связи отображаются подписью под названием («Требует: …», «Нужен для: …»).
"""
from collections import defaultdict
from typing import Dict, Iterator, List, Optional

from enums import ModStatus

# Категории = фильтры (карточки-сводки над списком)
CAT_ALL = "all"
CAT_UPDATES = "updates"
CAT_PROBLEMS = "problems"
CAT_NOT_FOUND = "not_found"
CAT_INSTALL = "install"

PROBLEM_STATUSES = (ModStatus.REQUIRED, ModStatus.INCOMPATIBLE, ModStatus.VERSION_CONFLICT,
                    ModStatus.DOWNLOAD_ERROR, ModStatus.HASH_ERROR, ModStatus.REPLACE_ERROR, ModStatus.CRITICAL_ERROR)

# Короткие подписи статусов и цветовая роль (ключ токена темы)
STATUS_STYLE: Dict[ModStatus, tuple] = {
    ModStatus.NOT_CHECKED: ("Не проверено", "text_faint"),
    ModStatus.UP_TO_DATE: ("Актуально", "text_muted"),
    ModStatus.UPDATE_AVAILABLE: ("Обновление", "accent"),
    ModStatus.READY_TO_INSTALL: ("К установке", "info"),
    ModStatus.NOT_FOUND: ("Не на Modrinth", "text_faint"),
    ModStatus.REQUIRED: ("Не установлен", "warning"),
    ModStatus.OPTIONAL: ("Опционально", "text_muted"),
    ModStatus.INCOMPATIBLE: ("Несовместим", "danger"),
    ModStatus.VERSION_CONFLICT: ("Конфликт версий", "danger"),
    ModStatus.DOWNLOADING: ("Скачивание", "info"),
    ModStatus.FINISHING: ("Установка…", "info"),
    ModStatus.UPDATED: ("Обновлено", "success"),
    ModStatus.INSTALLED: ("Установлено", "success"),
    ModStatus.CANCELLED: ("Отменено", "text_muted"),
    ModStatus.DOWNLOAD_ERROR: ("Ошибка загрузки", "danger"),
    ModStatus.HASH_ERROR: ("Ошибка хэша", "danger"),
    ModStatus.REPLACE_ERROR: ("Ошибка замены", "danger"),
    ModStatus.CRITICAL_ERROR: ("Ошибка", "danger"),
}


def status_of(entry: Dict) -> ModStatus:
    status = entry.get("status")
    try:
        return status if isinstance(status, ModStatus) else ModStatus(status)
    except ValueError:
        return ModStatus.NOT_CHECKED


def status_style(entry: Dict) -> tuple:
    return STATUS_STYLE.get(status_of(entry), (str(entry.get("status") or ""), "text_muted"))


def entry_key(entry: Dict):
    return entry.get("project_id") or entry.get("filepath") or entry.get("new_filename") or id(entry)


def iter_all_entries(mods_data: List[Dict]) -> Iterator[Dict]:
    for mod in mods_data:
        yield mod
        yield from mod.get("dependencies", [])


def build_view_items(mods_data: List[Dict]) -> List[Dict]:
    """Плоский список для отображения (без повторов по project_id) с вычисленными связями."""
    required_by: Dict[str, List[str]] = defaultdict(list)
    for mod in mods_data:
        for dep in mod.get("dependencies", []):
            if dep.get("project_id") and dep.get("type") in ("required", "optional"):
                required_by[dep["project_id"]].append(mod.get("name", "?"))

    items: List[Dict] = []
    seen = set()

    def add(entry: Dict):
        key = entry_key(entry)
        if key not in seen:
            seen.add(key)
            items.append(entry)

    for mod in mods_data:
        add(mod)
    for mod in mods_data:
        for dep in mod.get("dependencies", []):
            if dep.get("filepath") or dep.get("installed") or status_of(dep) == ModStatus.REQUIRED:
                add(dep)

    for entry in items:
        entry["_required_by"] = required_by.get(entry.get("project_id"), [])
        entry["_issues"] = entry_issues(entry)
    return items


def entry_issues(entry: Dict) -> List[str]:
    """Проблемы мода: недостающие обязательные и установленные несовместимые зависимости."""
    issues = []
    for dep in entry.get("dependencies", []):
        status = status_of(dep)
        if status == ModStatus.REQUIRED and not dep.get("filepath"):
            issues.append(f"нужен {dep.get('name')}")
        elif status == ModStatus.INCOMPATIBLE:
            issues.append(f"несовместим с {dep.get('name')}")
        elif status == ModStatus.VERSION_CONFLICT:
            issues.append(f"конфликт версий {dep.get('name')}")
    return issues


def is_problem(entry: Dict) -> bool:
    return status_of(entry) in PROBLEM_STATUSES or bool(entry.get("_issues"))


def matches_category(entry: Dict, category: str) -> bool:
    status = status_of(entry)
    if category == CAT_UPDATES:
        return status == ModStatus.UPDATE_AVAILABLE
    if category == CAT_PROBLEMS:
        return is_problem(entry)
    if category == CAT_NOT_FOUND:
        return status == ModStatus.NOT_FOUND
    if category == CAT_INSTALL:
        return status in (ModStatus.READY_TO_INSTALL, ModStatus.REQUIRED)
    return True


def category_counts(items: List[Dict]) -> Dict[str, int]:
    counts = {CAT_ALL: len(items)}
    for category in (CAT_UPDATES, CAT_PROBLEMS, CAT_NOT_FOUND, CAT_INSTALL):
        counts[category] = sum(1 for e in items if matches_category(e, category))
    return counts


def subtitle_for(entry: Dict) -> tuple[str, Optional[str]]:
    """Вторая строка под названием и её цветовая роль."""
    if entry.get("_issues"):
        text = ", ".join(entry["_issues"])
        return "⚠ " + text[:1].upper() + text[1:], "warning"
    if status_of(entry) == ModStatus.REQUIRED and entry.get("_required_by"):
        return "Нужен для: " + ", ".join(entry["_required_by"]), "warning"
    if entry.get("description"):
        return entry["description"], None
    if entry.get("_required_by"):
        return "Зависимость для: " + ", ".join(entry["_required_by"]), None
    path = entry.get("filepath") or entry.get("new_filename") or ""
    return path.replace("\\", "/").rsplit("/", 1)[-1], None


def search_text(entry: Dict) -> str:
    path = entry.get("filepath") or entry.get("new_filename") or ""
    return " ".join(str(x) for x in (entry.get("name"), entry.get("description"), path,
                                      entry.get("project_id"), entry.get("slug")) if x).lower()


SORT_PRIORITY = {ModStatus.UPDATE_AVAILABLE: 1, ModStatus.READY_TO_INSTALL: 2, ModStatus.NOT_FOUND: 4}


def status_sort_key(entry: Dict) -> int:
    if is_problem(entry):
        return 0
    return SORT_PRIORITY.get(status_of(entry), 3)
