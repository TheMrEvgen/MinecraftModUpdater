"""
История запусков игры сборки (без Qt): дошла ли до меню, вылетела ли, сколько грузилась.
По ней видно, после какой операции с модами игра перестала запускаться — её и предлагаем откатить.

Запуск — словарь: {"start": время, "menu": дошла до меню, "crashed": вылетела, "load": секунды или None}.
"""
import datetime
from typing import Dict, List, Optional

MAX_RUNS = 50


def record(history: Optional[List[Dict]], run: Dict) -> List[Dict]:
    """Добавляет запуск, храним последние MAX_RUNS."""
    runs = list(history or []) + [run]
    return runs[-MAX_RUNS:]


def ok(run: Dict) -> bool:
    return bool(run.get("menu")) and not run.get("crashed")


def stats(history: Optional[List[Dict]]) -> Dict:
    """Число запусков и вылетов, среднее время загрузки за последние 10 удачных запусков."""
    runs = list(history or [])
    loads = [r["load"] for r in runs if ok(r) and r.get("load")]
    recent = loads[-10:]
    return {
        "runs": len(runs),
        "crashes": sum(1 for r in runs if r.get("crashed")),
        "average_load": sum(recent) / len(recent) if recent else None,
        "last_load": loads[-1] if loads else None,
    }


def operation_time(manifest: Dict) -> Optional[float]:
    try:
        return datetime.datetime.fromisoformat(manifest.get("created", "")).timestamp()
    except ValueError:
        return None


def suspect_operation(history: Optional[List[Dict]], transactions: List[Dict]) -> Optional[Dict]:
    """
    Операция с модами, после которой игра только вылетает: до неё был удачный запуск, после — одни вылеты.
    transactions — из updater.list_transactions (новые первыми, только те, что ещё можно откатить).
    """
    runs = list(history or [])
    if not runs or not runs[-1].get("crashed"):
        return None
    last_ok = next((r for r in reversed(runs) if ok(r)), None)
    if last_ok is None:
        return None  # Игра ещё ни разу не запускалась удачно — сравнивать не с чем
    after_ok = [r for r in runs if r["start"] > last_ok["start"]]
    if not after_ok or not all(r.get("crashed") or not r.get("menu") for r in after_ok):
        return None
    first_bad = min(r["start"] for r in after_ok)
    for manifest in transactions:  # Самая свежая операция между удачным запуском и первым вылетом
        created = operation_time(manifest)
        if created is not None and last_ok["start"] < created <= first_bad:
            return manifest
    return None


def changed_mods(manifest: Dict, limit: int = 4) -> str:
    """«sodium-0.6, lithium-0.14 (+2)» — что поменяла операция."""
    files = list(manifest.get("installed") or [])
    if not files:  # Только удаление: в бэкапе записи {"name": файл, "backup": имя в бэкапе}
        files = [e.get("name", "") if isinstance(e, dict) else str(e) for e in manifest.get("backed_up") or []]
    names = [name[:-4] if name.lower().endswith(".jar") else name for name in files]
    shown = ", ".join(names[:limit])
    return shown + (f" (+{len(names) - limit})" if len(names) > limit else "")
