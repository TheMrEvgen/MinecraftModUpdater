"""Статистика сборки для страницы «Статистика» (без Qt)."""
from typing import Dict, List, Optional

from query import ABANDONED_DAYS, days_since
from view_model import entry_tags, group_of, side_tag
from i18n import tr

SIDE_TITLES = {"side:client": tr("Только клиент"), "side:server": tr("Только сервер"), "side:both": tr("Клиент и сервер"),
               "side:any": tr("Клиент или сервер"), "unknown": tr("Неизвестно")}


def build_stats(entries: List[Dict], custom_tags: Optional[Dict] = None, top: int = 10) -> Dict:
    installed = [e for e in entries if e.get("filepath")]
    total = sum(e.get("size") or 0 for e in installed)

    by_category: Dict[str, List] = {}
    for entry in installed:
        cats = [title for key, title in entry_tags(entry, custom_tags) if key.startswith("cat:")] or [tr("без категории")]
        for title in cats:
            slot = by_category.setdefault(title, [0, 0])
            slot[0] += 1
            slot[1] += entry.get("size") or 0

    sides: Dict[str, int] = {}
    for entry in installed:
        key = (side_tag(entry) or ("unknown",))[0]
        sides[key] = sides.get(key, 0) + 1

    abandoned = []
    for entry in installed:
        age = days_since(entry.get("project_updated"))
        if age is not None and age > ABANDONED_DAYS:
            abandoned.append((entry, int(age)))
    abandoned.sort(key=lambda pair: -pair[1])

    groups: Dict[str, int] = {}
    for entry in installed:
        groups[group_of(entry)] = groups.get(group_of(entry), 0) + 1

    return {
        "count": len(installed),
        "enabled": sum(1 for e in installed if not e.get("disabled")),
        "disabled": sum(1 for e in installed if e.get("disabled")),
        "total_size": total,
        "update_size": sum(e.get("new_size") or 0 for e in installed if e.get("download_url") and not e.get("_pinned")),
        "libraries": sum(1 for e in installed if "library" in (e.get("categories") or [])),
        "groups": groups,
        "by_category": sorted(((t, c, s) for t, (c, s) in by_category.items()), key=lambda x: (-x[2], x[0])),
        "heaviest": sorted(installed, key=lambda e: -(e.get("size") or 0))[:top],
        "sides": sorted(((SIDE_TITLES.get(k, k), n) for k, n in sides.items()), key=lambda x: -x[1]),
        "abandoned": abandoned,
    }
