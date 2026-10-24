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
from i18n import language, tr

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
    ModStatus.NOT_CHECKED: (tr("Не проверено"), "text_faint"),
    ModStatus.UP_TO_DATE: (tr("Актуально"), "text_muted"),
    ModStatus.UPDATE_AVAILABLE: (tr("Обновление"), "accent"),
    ModStatus.READY_TO_INSTALL: (tr("К установке"), "info"),
    ModStatus.NOT_FOUND: (tr("Не на Modrinth"), "text_faint"),
    ModStatus.REQUIRED: (tr("Не установлен"), "warning"),
    ModStatus.OPTIONAL: (tr("Опционально"), "text_muted"),
    ModStatus.INCOMPATIBLE: (tr("Несовместим"), "danger"),
    ModStatus.VERSION_CONFLICT: (tr("Конфликт версий"), "danger"),
    ModStatus.DOWNLOADING: (tr("Скачивание"), "info"),
    ModStatus.FINISHING: (tr("Установка…"), "info"),
    ModStatus.UPDATED: (tr("Обновлено"), "success"),
    ModStatus.INSTALLED: (tr("Установлено"), "success"),
    ModStatus.CANCELLED: (tr("Отменено"), "text_muted"),
    ModStatus.DOWNLOAD_ERROR: (tr("Ошибка загрузки"), "danger"),
    ModStatus.HASH_ERROR: (tr("Ошибка хэша"), "danger"),
    ModStatus.REPLACE_ERROR: (tr("Ошибка замены"), "danger"),
    ModStatus.CRITICAL_ERROR: (tr("Ошибка"), "danger"),
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
        return tr("Выключен"), "text_faint"
    if entry.get("_pinned") and status == ModStatus.UPDATE_AVAILABLE:
        return tr("Закреплён"), "text_muted"
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


def new_version_age_days(entry: Dict) -> Optional[float]:
    """Сколько дней назад вышла предлагаемая версия (None — дата неизвестна)."""
    import datetime
    published = (entry.get("_latest_info") or {}).get("date_published")
    if not published:
        return None
    try:
        then = datetime.datetime.fromisoformat(str(published).replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=datetime.timezone.utc)
    return (datetime.datetime.now(datetime.timezone.utc) - then).total_seconds() / 86400


def waiting_days(entry: Dict, min_age_days: float) -> int:
    """Сколько дней ещё ждать обновлению («Ставить версии не раньше чем через N дней»); 0 — ждать не нужно.
    Касается только обновлений установленных модов: недостающие зависимости ставятся сразу."""
    if not min_age_days or not entry.get("filepath") or not entry.get("download_url"):
        return 0
    age = new_version_age_days(entry)
    if age is None or age >= min_age_days:
        return 0
    return max(1, int(min_age_days - age + 0.999))


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
            result[id(present[a])].append(tr('несовместим с {0}', present[b].get('name')))
            result[id(present[b])].append(tr('несовместим с {0}', present[a].get('name')))
    return result


# Режимы списка (настройка «Вид списка»)
VIEW_FLAT = "flat"   # Все моды и зависимости одним списком
VIEW_MINE = "mine"   # Только установленные моды — без недостающих зависимостей
VIEW_TREE = "tree"   # Моды с раскрывающимися зависимостями
VIEW_MODES = (VIEW_FLAT, VIEW_MINE, VIEW_TREE)
DEP_TYPE_LABELS = {"required": tr("обязательная"), "optional": tr("необязательная"), "incompatible": tr("несовместимая")}


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
        issues.append(tr('лишняя копия — основной файл {0}', entry['duplicate_of']))
    elif entry.get("duplicates"):
        issues.append(tr('дубликат: {0}', ', '.join(entry['duplicates'])))
    if entry.get("disabled"):
        return issues  # Выключенный мод не загружается — его зависимости не важны
    for dep in entry.get("dependencies", []):
        status = status_of(dep)
        if status == ModStatus.REQUIRED and not dep.get("filepath"):
            issues.append(tr('нужен {0}', dep.get('name')))
        elif status == ModStatus.INCOMPATIBLE:
            issues.append(tr('несовместим с {0}', dep.get('name')))
        elif status == ModStatus.VERSION_CONFLICT:
            issues.append(tr('конфликт версий {0}', dep.get('name')))
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
    if entry.get("manual_url") and not entry.get("download_url") and entry.get("new_version"):
        return tr("Новая версия {0} — автор разрешает скачивать её только с сайта CurseForge", entry["new_version"]), "info"
    if status_of(entry) == ModStatus.REQUIRED and entry.get("_required_by"):
        return tr("Нужен для: ") + ", ".join(entry["_required_by"]), "warning"
    if entry.get("description"):
        return entry["description"], None
    if entry.get("_required_by"):
        return tr("Зависимость для: ") + ", ".join(entry["_required_by"]), None
    path = entry.get("filepath") or entry.get("new_filename") or ""
    return path.replace("\\", "/").rsplit("/", 1)[-1], None


def search_text(entry: Dict, custom_tags: Optional[Dict[str, List[str]]] = None) -> str:
    path = entry.get("filepath") or entry.get("new_filename") or ""
    tags = [title for _, title in entry_tags(entry, custom_tags)]
    return " ".join(str(x) for x in (entry.get("name"), entry.get("description"), path, entry.get("_note"),
                                      entry.get("project_id"), entry.get("slug"), *tags) if x).lower()


# --- Группы (заголовки в списке при сортировке «Сначала важные») ---

GROUPS = (("problems", tr("Проблемы")), ("updates", tr("Обновления")), ("install", tr("К установке")), ("ok", tr("Актуальные")),
          ("not_found", tr("Нет в каталогах")), ("disabled", tr("Выключенные")))
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
    for unit in (tr("Б"), tr("КБ"), tr("МБ"), tr("ГБ")):
        if size < 1024 or unit == tr("ГБ"):
            return f"{size:.0f} {unit}" if unit in (tr("Б"), tr("КБ")) else f"{size:.1f} {unit}".replace(".", "," if language() == "ru" else ".")
        size /= 1024
    return ""


# --- Метки ---

CATEGORY_LABELS = {
    "adventure": tr("приключения"), "cursed": tr("необычное"), "decoration": tr("декор"), "economy": tr("экономика"),
    "equipment": tr("снаряжение"), "food": tr("еда"), "game-mechanics": tr("механики"), "library": tr("библиотека"),
    "magic": tr("магия"), "management": tr("управление"), "minigame": tr("мини-игры"), "mobs": tr("мобы"),
    "optimization": tr("оптимизация"), "social": tr("общение"), "storage": tr("хранение"), "technology": tr("технологии"),
    "transportation": tr("транспорт"), "utility": tr("утилиты"), "worldgen": tr("генерация мира"),
    # Шейдеры и ресурспаки
    "atmosphere": tr("атмосфера"), "bloom": tr("свечение"), "cartoon": tr("мультяшные"), "colored-lighting": tr("цветной свет"),
    "fantasy": tr("фэнтези"), "foliage": tr("растительность"), "high": tr("мощные ПК"), "low": tr("слабые ПК"), "medium": tr("средние ПК"),
    "path-tracing": tr("трассировка пути"), "pbr": "PBR", "potato": tr("очень слабые ПК"), "realistic": tr("реализм"),
    "reflections": tr("отражения"), "screenshot": tr("для скриншотов"), "semi-realistic": tr("полуреализм"),
    "shadows": tr("тени"), "vanilla-like": tr("как в ванилле"), "combat": tr("бой"), "core-shaders": tr("core-шейдеры"),
    "gui": tr("интерфейс"), "audio": tr("звуки"), "blocks": tr("блоки"), "entities": tr("существа"), "environment": tr("окружение"),
    "fonts": tr("шрифты"), "items": tr("предметы"), "locale": tr("перевод"), "models": tr("модели"), "simplistic": tr("минимализм"),
    "themed": tr("тематические"), "tweaks": tr("твики"), "utility-pack": tr("полезное"),
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
        return "side:client", tr("только клиент")
    if client == "unsupported":
        return "side:server", tr("только сервер")
    if client == "required" and server == "required":
        return "side:both", tr("клиент и сервер")
    return "side:any", tr("клиент или сервер")


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
