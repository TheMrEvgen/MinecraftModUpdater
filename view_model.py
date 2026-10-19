"""
Подготовка данных сканирования для списка модов (без Qt).

Сканер возвращает дерево «мод -> зависимости». В интерфейсе показывается плоский список:
все установленные моды (включая установленные зависимости) и недостающие обязательные
зависимости. Связи отображаются подписью под названием («Требует: …», «Нужен для: …»).
"""
import os
from collections import defaultdict
from typing import Dict, Iterable, Iterator, List, Optional

from content import enabled_name
from enums import ModStatus

# Категории = фильтры (карточки-сводки над списком)
CAT_ALL = "all"
CAT_UPDATES = "updates"
CAT_PROBLEMS = "problems"
CAT_NOT_FOUND = "not_found"
CAT_INSTALL = "install"
CAT_DISABLED = "disabled"

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
    status = status_of(entry)
    if entry.get("disabled") and status not in (ModStatus.DOWNLOADING, ModStatus.FINISHING):
        return "Выключен", "text_faint"
    if entry.get("_pinned") and status == ModStatus.UPDATE_AVAILABLE:
        return "Закреплён", "text_muted"
    return STATUS_STYLE.get(status, (str(entry.get("status") or ""), "text_muted"))


def entry_key(entry: Dict):
    if entry.get("duplicate_of"):  # Лишняя копия мода — отдельная строка со своим файлом
        return entry.get("filepath") or id(entry)
    return entry.get("project_id") or entry.get("filepath") or entry.get("new_filename") or id(entry)


def pin_key(entry: Dict) -> Optional[str]:
    """Ключ закрепления версии: проект Modrinth или имя файла (для модов не с Modrinth)."""
    if entry.get("project_id"):
        return entry["project_id"]
    path = entry.get("filepath")
    return enabled_name(os.path.basename(path)) if path else None


def is_updatable(entry: Dict) -> bool:
    """Мод попадает в «Обновить все»: есть что скачать, не закреплён и не выключен."""
    return bool(entry.get("download_url")) and not entry.get("_pinned") and not entry.get("disabled")


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

    conflicts = find_known_conflicts(items)
    for entry in items:
        entry["_required_by"] = required_by.get(entry.get("project_id"), [])
        entry["_issues"] = entry_issues(entry) + conflicts.get(id(entry), [])
    return items


# Известные несовместимые пары модов, которые Modrinth не отмечает (часто их нет на Modrinth вовсе).
# Сравнение по slug проекта, а для модов не с Modrinth — по названию/имени файла.
KNOWN_CONFLICTS = (
    ("sodium", "optifine"), ("sodium", "optifabric"), ("iris", "optifine"), ("iris", "optifabric"),
    ("embeddium", "optifine"), ("rubidium", "optifine"), ("oculus", "optifine"),
    ("sodium", "embeddium"), ("sodium", "rubidium"), ("embeddium", "rubidium"),
    ("starlight", "phosphor"), ("lithium", "canary"), ("lithium", "radium"),
)
_CONFLICT_IDS = {name for pair in KNOWN_CONFLICTS for name in pair}


def _conflict_ids(entry: Dict) -> set:
    """Какие из известных модов соответствуют записи (по slug или по названию/файлу)."""
    if entry.get("slug"):
        return {entry["slug"]} & _CONFLICT_IDS
    text = " ".join(str(x) for x in (entry.get("name"), os.path.basename(entry.get("filepath") or "")) if x).lower()
    return {name for name in _CONFLICT_IDS if name in text.replace(" ", "")}


def find_known_conflicts(items: Iterable[Dict]) -> Dict[int, List[str]]:
    """id(запись) -> предупреждения о несовместимых модах, установленных одновременно (выключенные не считаются)."""
    present: Dict[str, Dict] = {}
    for entry in items:
        if entry.get("filepath") and not entry.get("disabled"):
            for name in _conflict_ids(entry):
                present.setdefault(name, entry)
    result: Dict[int, List[str]] = defaultdict(list)
    for a, b in KNOWN_CONFLICTS:
        if a in present and b in present and present[a] is not present[b]:
            result[id(present[a])].append(f"несовместим с {present[b].get('name')}")
            result[id(present[b])].append(f"несовместим с {present[a].get('name')}")
    return result


# Режимы списка (настройка «Вид списка»)
VIEW_FLAT = "flat"   # Все моды и зависимости одним списком
VIEW_MINE = "mine"   # Только установленные моды — без недостающих зависимостей
VIEW_TREE = "tree"   # Моды с раскрывающимися зависимостями
VIEW_MODES = (VIEW_FLAT, VIEW_MINE, VIEW_TREE)
DEP_TYPE_LABELS = {"required": "обязательная", "optional": "необязательная", "incompatible": "несовместимая"}


def build_rows(mods_data: List[Dict], mode: str = VIEW_FLAT) -> tuple[List[tuple], List[Dict]]:
    """
    Строки списка для режима и записи для подсчёта карточек-сводок.
    Строка — (запись, meta); meta: depth (0 — мод, 1 — зависимость), parent_row, dep_type, children (номера строк).
    В дереве зависимость, которая есть и в плоском списке, — тот же словарь: отметка и статус у них общие.
    """
    items = build_view_items(mods_data)
    if mode == VIEW_MINE:
        mine = [e for e in items if e.get("filepath")]
        if mine:  # В режиме импорта своих файлов нет — показываем всё
            return [(e, {"depth": 0}) for e in mine], mine
    if mode != VIEW_TREE:
        return [(e, {"depth": 0}) for e in items], items

    canonical = {entry_key(e): e for e in items}
    rows: List[tuple] = []
    for mod in mods_data:
        parent = canonical.get(entry_key(mod), mod)
        parent_row = len(rows)
        meta = {"depth": 0, "children": []}
        rows.append((parent, meta))
        # В том же порядке, что и при показе (по имени): так известна последняя ветка дерева
        for dep in sorted(mod.get("dependencies", []), key=lambda d: str(d.get("name", "")).lower()):
            child = canonical.get(entry_key(dep), dep)
            if child is dep and "_issues" not in dep:
                dep["_required_by"], dep["_issues"] = [mod.get("name", "?")], entry_issues(dep)
            meta["children"].append(len(rows))
            rows.append((child, {"depth": 1, "parent_row": parent_row, "dep_type": dep.get("type")}))
        if meta["children"]:
            rows[meta["children"][-1]][1]["last"] = True
    return rows, items


def entry_issues(entry: Dict) -> List[str]:
    """Проблемы мода: недостающие обязательные и установленные несовместимые зависимости."""
    issues = []
    if entry.get("duplicate_of"):
        issues.append(f"лишняя копия — основной файл {entry['duplicate_of']}")
    elif entry.get("duplicates"):
        issues.append(f"дубликат: {', '.join(entry['duplicates'])}")
    if entry.get("disabled"):
        return issues  # Выключенный мод не загружается — его зависимости не важны
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
        return status == ModStatus.UPDATE_AVAILABLE and not entry.get("_pinned") and not entry.get("disabled")
    if category == CAT_DISABLED:
        return bool(entry.get("disabled"))
    if category == CAT_PROBLEMS:
        return is_problem(entry)
    if category == CAT_NOT_FOUND:
        return status == ModStatus.NOT_FOUND
    if category == CAT_INSTALL:
        return status in (ModStatus.READY_TO_INSTALL, ModStatus.REQUIRED)
    return True


def category_counts(items: List[Dict]) -> Dict[str, int]:
    counts = {CAT_ALL: len(items)}
    for category in (CAT_UPDATES, CAT_PROBLEMS, CAT_NOT_FOUND, CAT_INSTALL, CAT_DISABLED):
        counts[category] = sum(1 for e in items if matches_category(e, category))
    return counts


def subtitle_for(entry: Dict) -> tuple[str, Optional[str]]:
    """Вторая строка под названием и её цветовая роль (всегда в одну строку)."""
    text, role = _subtitle(entry)
    return " ".join(text.split()), role


def _subtitle(entry: Dict) -> tuple[str, Optional[str]]:
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


def search_text(entry: Dict, custom_tags: Optional[Dict[str, List[str]]] = None) -> str:
    path = entry.get("filepath") or entry.get("new_filename") or ""
    tags = [title for _, title in entry_tags(entry, custom_tags)]
    return " ".join(str(x) for x in (entry.get("name"), entry.get("description"), path,
                                      entry.get("project_id"), entry.get("slug"), *tags) if x).lower()


# --- Группы (заголовки в списке при сортировке «Сначала важные») ---

GROUPS = (("problems", "Проблемы"), ("updates", "Обновления"), ("install", "К установке"), ("ok", "Актуальные"),
          ("not_found", "Нет на Modrinth"), ("disabled", "Выключенные"))
GROUP_TITLES = dict(GROUPS)
_GROUP_ORDER = {key: i for i, (key, _) in enumerate(GROUPS)}


def group_of(entry: Dict) -> str:
    status = status_of(entry)
    if entry.get("disabled"):
        return "disabled"
    if is_problem(entry):
        return "problems"
    if status == ModStatus.UPDATE_AVAILABLE and not entry.get("_pinned"):
        return "updates"
    if status == ModStatus.READY_TO_INSTALL:
        return "install"
    if status == ModStatus.NOT_FOUND:
        return "not_found"
    return "ok"


def status_sort_key(entry: Dict) -> int:
    return _GROUP_ORDER[group_of(entry)]


def update_date(entry: Dict) -> str:
    """Дата для сортировки «Недавно обновлённые»: новая версия, иначе установленная."""
    info = entry.get("_latest_info") or entry.get("installed_info") or {}
    return info.get("date_published") or ""


def format_size(size: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{size:.0f} {unit}" if unit in ("Б", "КБ") else f"{size:.1f} {unit}".replace(".", ",")
        size /= 1024
    return ""


# --- Метки ---

CATEGORY_LABELS = {
    "adventure": "приключения", "cursed": "необычное", "decoration": "декор", "economy": "экономика",
    "equipment": "снаряжение", "food": "еда", "game-mechanics": "механики", "library": "библиотека",
    "magic": "магия", "management": "управление", "minigame": "мини-игры", "mobs": "мобы",
    "optimization": "оптимизация", "social": "общение", "storage": "хранение", "technology": "технологии",
    "transportation": "транспорт", "utility": "утилиты", "worldgen": "генерация мира",
    # Шейдеры и ресурспаки
    "atmosphere": "атмосфера", "bloom": "свечение", "cartoon": "мультяшные", "colored-lighting": "цветной свет",
    "fantasy": "фэнтези", "foliage": "растительность", "high": "мощные ПК", "low": "слабые ПК", "medium": "средние ПК",
    "path-tracing": "трассировка пути", "pbr": "PBR", "potato": "очень слабые ПК", "realistic": "реализм",
    "reflections": "отражения", "screenshot": "для скриншотов", "semi-realistic": "полуреализм",
    "shadows": "тени", "vanilla-like": "как в ванилле", "combat": "бой", "core-shaders": "core-шейдеры",
    "gui": "интерфейс", "audio": "звуки", "blocks": "блоки", "entities": "существа", "environment": "окружение",
    "fonts": "шрифты", "items": "предметы", "locale": "перевод", "models": "модели", "simplistic": "минимализм",
    "themed": "тематические", "tweaks": "твики", "utility-pack": "полезное",
}
LOADER_CATEGORIES = {"fabric", "forge", "neoforge", "quilt", "liteloader", "modloader", "rift", "iris", "optifine",
                     "babric", "bta-babric", "legacy-fabric", "ornithe", "nilloader", "java-agent", "geyser",
                     "canvas", "vanilla", "minecraft", "datapack", "bukkit", "paper", "spigot", "folia", "sponge",
                     "velocity", "waterfall", "bungeecord", "purpur"}


def side_tag(entry: Dict) -> Optional[tuple]:
    client, server = entry.get("client_side"), entry.get("server_side")
    if not client or not server:
        return None
    if server == "unsupported":
        return "side:client", "только клиент"
    if client == "unsupported":
        return "side:server", "только сервер"
    if client == "required" and server == "required":
        return "side:both", "клиент и сервер"
    return "side:any", "клиент или сервер"


def entry_tags(entry: Dict, custom_tags: Optional[Dict[str, List[str]]] = None) -> List[tuple]:
    """Метки мода: (ключ, подпись). Категории Modrinth, сторона (клиент/сервер) и метки пользователя."""
    tags = [(f"cat:{c}", CATEGORY_LABELS.get(c, c)) for c in entry.get("categories") or [] if c not in LOADER_CATEGORIES]
    side = side_tag(entry)
    if side:
        tags.append(side)
    key = pin_key(entry)
    for tag in (custom_tags or {}).get(key or "", []):
        tags.append((f"user:{tag}", tag))
    return tags


def collect_tags(items: Iterable[Dict], custom_tags: Optional[Dict[str, List[str]]] = None) -> List[tuple]:
    """Все метки в списке: (ключ, подпись, число модов), по убыванию числа."""
    counts: Dict[str, List] = {}
    for entry in items:
        for key, title in entry_tags(entry, custom_tags):
            counts.setdefault(key, [title, 0])[1] += 1
    return sorted(((k, v[0], v[1]) for k, v in counts.items()), key=lambda t: (-t[2], t[1]))
