"""
Готовность сборки к новой версии Minecraft (без Qt): вышла ли версия новее текущей и сколько модов
уже выпущено под неё. Результат хранится в профиле, проверка повторяется не чаще раза в CHECK_INTERVAL.
"""
import time
from typing import Dict, List, Optional

from content import MODS
from enums import UpdateChannel
from mod_updater_logic import get_game_versions
from scanner import check_target_version

CHECK_INTERVAL = 12 * 3600


def newer_release(current: str, releases: List[str]) -> Optional[str]:
    """Самая свежая версия, вышедшая после current (список — новые первыми, как отдаёт Modrinth)."""
    if not current or current not in releases:
        return None
    index = releases.index(current)
    return releases[0] if index > 0 else None


def check_readiness(entries: List[Dict], current: str, loader: str, channel: UpdateChannel) -> Optional[Dict]:
    """None — новее версии нет. Иначе {target, ready, total, missing: [названия], checked}."""
    target = newer_release(current, get_game_versions())
    if not target:
        return None
    installed = [e for e in entries if e.get("filepath") and e.get("sha1")]
    results = check_target_version(installed, target, loader, channel, MODS)
    missing = [r["entry"].get("name") for r in results if not r["version"]]
    return {"target": target, "ready": len(results) - len(missing), "total": len(results), "missing": missing,
            "checked": time.time()}


def needs_check(saved: Optional[Dict], now: Optional[float] = None) -> bool:
    return not saved or (now or time.time()) - saved.get("checked", 0) > CHECK_INTERVAL
