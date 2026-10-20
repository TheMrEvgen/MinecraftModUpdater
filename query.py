"""
Поиск с условиями в строке поиска (без Qt):

    sodium                      — обычный поиск по названию, описанию, файлу, меткам и заметкам
    tag:оптимизация             — метка (часть названия метки)
    side:client | server | both — сторона
    is:fav | pinned | disabled | new | note | abandoned | lib — признак
    status:update | problem | ok | notfound | install
    size>5mb   size<500kb       — размер файла
    age>1y     age<30d          — сколько прошло с последнего обновления мода автором
    -tag:библиотека             — минус в начале отрицает условие
"""
import datetime
import re
import shlex
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from i18n import tr
from view_model import entry_tags, group_of, search_text, side_tag

SIZE_UNITS = {"b": 1, "kb": 1024, "кб": 1024, "mb": 1024 ** 2, "мб": 1024 ** 2, "gb": 1024 ** 3, "гб": 1024 ** 3}
AGE_UNITS = {"d": 1, "д": 1, "w": 7, "н": 7, "m": 30, "м": 30, "y": 365, "г": 365}
_COMPARE_RE = re.compile(r"^(size|age)(>=|<=|>|<|=)(\d+(?:[.,]\d+)?)([a-zа-я]*)$", re.IGNORECASE)
ABANDONED_DAYS = 365

# Подсказки для автодополнения в строке поиска
QUERY_HINTS = [
    ("tag:", tr("метка")), ("side:client", tr("только клиентские")), ("side:server", tr("нужные на сервере")),
    ("side:both", tr("клиент и сервер")), ("is:fav", tr("избранные")), ("is:pinned", tr("закреплённые")),
    ("is:disabled", tr("выключенные")), ("is:new", tr("новые обновления с прошлого раза")), ("is:note", tr("с заметкой")),
    ("is:abandoned", tr("давно не обновлялись")), ("is:lib", tr("библиотеки")), ("status:update", tr("есть обновление")),
    ("status:problem", tr("с проблемами")), ("status:notfound", tr("нет на Modrinth")), ("size>5mb", tr("больше 5 МБ")),
    ("age>1y", tr("не обновлялись больше года")), ("-tag:", tr("без метки")),
]


def days_since(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        then = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.datetime.now(datetime.timezone.utc) - then).total_seconds() / 86400


def is_abandoned(entry: Dict, days: int = ABANDONED_DAYS) -> bool:
    age = days_since(entry.get("project_updated"))
    return age is not None and age > days


def _compare(op: str, value: float, limit: float) -> bool:
    return {">": value > limit, "<": value < limit, ">=": value >= limit, "<=": value <= limit, "=": value == limit}[op]


@dataclass
class Query:
    words: List[str] = field(default_factory=list)
    conditions: List[tuple] = field(default_factory=list)  # (отрицание, проверка)

    @property
    def empty(self) -> bool:
        return not self.words and not self.conditions

    def matches(self, entry: Dict, custom_tags: Optional[Dict] = None) -> bool:
        if self.words:
            text = search_text(entry, custom_tags)
            if not all(word in text for word in self.words):
                return False
        return all(check(entry, custom_tags) != negate for negate, check in self.conditions)


def _flag_check(flag: str) -> Optional[Callable]:
    checks = {
        "fav": lambda e, _: bool(e.get("_favorite")), "favorite": lambda e, _: bool(e.get("_favorite")),
        "pinned": lambda e, _: bool(e.get("_pinned")), "disabled": lambda e, _: bool(e.get("disabled")),
        "new": lambda e, _: bool(e.get("_new")), "note": lambda e, _: bool(e.get("_note")),
        "abandoned": lambda e, _: is_abandoned(e), "lib": lambda e, _: "library" in (e.get("categories") or []),
        "installed": lambda e, _: bool(e.get("filepath")),
    }
    return checks.get(flag)


STATUS_GROUPS = {"update": "updates", "updates": "updates", "problem": "problems", "problems": "problems",
                 "ok": "ok", "notfound": "not_found", "install": "install", "disabled": "disabled"}


def _condition(key: str, value: str) -> Optional[Callable]:
    value = value.lower()
    if key == "tag":
        return lambda e, tags: any(value in title.lower() or value in k.lower() for k, title in entry_tags(e, tags))
    if key == "side":
        wanted = {"client": "side:client", "server": "side:server", "both": "side:both", "any": "side:any",
                  "клиент": "side:client", "сервер": "side:server"}.get(value)
        if value == "server":  # «нужен на сервере»: только сервер или обе стороны
            return lambda e, _: (side_tag(e) or ("",))[0] in ("side:server", "side:both")
        return (lambda e, _: (side_tag(e) or ("",))[0] == wanted) if wanted else None
    if key in ("is", "has"):
        return _flag_check(value)
    if key == "status":
        group = STATUS_GROUPS.get(value)
        return (lambda e, _: group_of(e) == group) if group else None
    if key == "version":
        return lambda e, _: value in str(e.get("current_version") or "").lower()
    return None


def parse_query(text: str) -> Query:
    try:
        tokens = shlex.split(text or "")
    except ValueError:  # Незакрытая кавычка — разбираем по пробелам
        tokens = (text or "").split()
    query = Query()
    for token in tokens:
        negate = token.startswith("-") and len(token) > 1
        body = token[1:] if negate else token
        match = _COMPARE_RE.match(body.lower())
        if match:
            field_name, op, number, unit = match.groups()
            amount = float(number.replace(",", "."))
            if field_name == "size":
                limit = amount * SIZE_UNITS.get(unit or "mb", 1024 ** 2)
                query.conditions.append((negate, lambda e, _, o=op, lim=limit:
                                         _compare(o, float(e.get("size") or e.get("new_size") or 0), lim)))
            else:
                limit = amount * AGE_UNITS.get(unit or "d", 1)
                query.conditions.append((negate, lambda e, _, o=op, lim=limit:
                                         days_since(e.get("project_updated")) is not None
                                         and _compare(o, days_since(e.get("project_updated")), lim)))
            continue
        if ":" in body:
            key, _, value = body.partition(":")
            check = _condition(key.lower(), value) if value else None
            if check:
                query.conditions.append((negate, check))
                continue
        query.words.append(token.lower())
    return query
