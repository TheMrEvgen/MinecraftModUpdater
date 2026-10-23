"""
Проверка обновлений модов без окна (без Qt): для консольного режима и фоновой проверки из трея.

    check_all(config)                — обновления во всех сборках
    new_updates(results, notified)   — что ещё не показывали в уведомлении
"""
import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import config as app_config
from content import MODS
from enums import ModStatus, UpdateChannel
from game_detection import detect_game_versions
from i18n import tr


@dataclass
class ProfileUpdates:
    name: str
    updates: List[Dict] = field(default_factory=list)  # Записи модов с новой версией

    def keys(self) -> List[str]:
        """Что именно обновится: «проект:новая версия» — чтобы не напоминать об уже показанном."""
        return sorted(f"{u.get('project_id')}:{u.get('new_version')}" for u in self.updates)


def resolve_versions(profile: Dict, game_version: Optional[str] = None,
                     loader: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """Версия игры и загрузчик сборки: из папки versions (если не задано вручную), явные значения важнее."""
    found_version, found_loader = profile.get("game_version"), profile.get("loader")
    if not profile.get("manual_mode"):
        detected = detect_game_versions(profile.get("mods_path", ""))
        if detected.best_guess:
            found_version, found_loader = detected.best_guess
    return game_version or found_version, loader or found_loader


def collect_updates(mods_data: List[Dict], include_deps: bool, pinned: Iterable[str] = ()) -> List[Dict]:
    """Моды, которые поставит «Обновить все»: обновления (и недостающие зависимости), кроме закреплённых и выключенных."""
    from view_model import pin_key
    pinned = set(pinned)
    statuses = {ModStatus.UPDATE_AVAILABLE, ModStatus.READY_TO_INSTALL}
    if include_deps:
        statuses.add(ModStatus.REQUIRED)
    result = {}
    for mod in mods_data:
        for entry in [mod] + mod.get('dependencies', []):
            pid = entry.get('project_id')
            if (pid and pid not in result and entry.get('download_url') and entry.get('status') in statuses
                    and not entry.get('disabled') and pin_key(entry) not in pinned):
                result[pid] = entry
    return list(result.values())


def check_profile(name: str, profile: Dict, cache_file: Optional[str] = None,
                  is_cancelled: Callable[[], bool] = lambda: False) -> Optional[ProfileUpdates]:
    """Обновления модов одной сборки; None — сборку проверить нельзя (нет папки, версии игры)."""
    from scanner import scan_mods
    mods_path = MODS.path(profile.get("mods_path") or "")
    if not profile.get("mods_path") or not os.path.isdir(mods_path):
        return None
    game_version, loader = resolve_versions(profile)
    if not game_version or not loader:
        return None
    channel = UpdateChannel.from_value(profile.get("channel"), app_config.DEFAULT_CHANNEL)
    mods_data = scan_mods(mods_path, game_version, loader, cache_file or app_config.CACHE_FILE, channel,
                          is_cancelled=is_cancelled)
    updates = [u for u in collect_updates(mods_data, include_deps=False, pinned=profile.get("pinned") or {})
               if u.get("status") == ModStatus.UPDATE_AVAILABLE]
    return ProfileUpdates(name, updates)


def check_all(config: Dict, cache_file: Optional[str] = None,
              is_cancelled: Callable[[], bool] = lambda: False) -> List[ProfileUpdates]:
    """Все сборки по очереди. Ошибка одной сборки не мешает остальным."""
    results = []
    for name, profile in (config.get("profiles") or {}).items():
        if is_cancelled():
            break
        try:
            result = check_profile(name, profile, cache_file, is_cancelled)
        except Exception as e:  # Сеть, битая папка — пропускаем сборку, остальные проверяем
            if is_cancelled():
                break
            logging.warning(tr("Фоновая проверка «{0}» не удалась: {1}", name, e))
            continue
        if result is not None:
            results.append(result)
    return results


def new_updates(results: List[ProfileUpdates], notified: Dict[str, List[str]]) -> List[ProfileUpdates]:
    """Сборки, где появились обновления, о которых ещё не сообщали."""
    fresh = []
    for result in results:
        unseen = set(result.keys()) - set(notified.get(result.name) or [])
        if unseen:
            fresh.append(result)
    return fresh


def summary(results: List[ProfileUpdates], limit: int = 3) -> str:
    """«Сборка: 3, Другая: 1» — для уведомления и подсказки значка."""
    parts = [tr("«{0}»: {1}", r.name, len(r.updates)) for r in results if r.updates][:limit]
    more = len([r for r in results if r.updates]) - limit
    return ", ".join(parts) + (tr(" и ещё {0}", more) if more > 0 else "")
