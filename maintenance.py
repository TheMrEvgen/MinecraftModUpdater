"""
Обслуживание сборки (без Qt): проверка целостности, место под бэкапы и их очистка,
клонирование и сравнение сборок, наборы модов (какие включены).
"""
import datetime
import logging
import os
import shutil
import zipfile
from typing import Dict, Iterable, List, Optional, Set

from content import MODS, ContentType, enabled_name, is_disabled
from updater import BACKUP_DIR_NAME, list_snapshots, snapshots_root, transactions_root
from i18n import tr

PARTIAL_SUFFIXES = (".part", ".tmp")


# --- Проверка целостности ---

def check_integrity(folder: str, content: ContentType = MODS) -> Dict[str, List[str]]:
    """
    broken  — файлы, которые не открываются как архив (игра на них упадёт);
    partial — остатки прерванной загрузки (*.part, *.tmp);
    empty   — файлы нулевого размера.
    """
    result = {"broken": [], "partial": [], "empty": []}
    if not os.path.isdir(folder):
        return result
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        if name.lower().endswith(PARTIAL_SUFFIXES):
            result["partial"].append(path)
        elif content.matches(name):
            if os.path.getsize(path) == 0:
                result["empty"].append(path)
                continue
            try:
                with zipfile.ZipFile(path) as z:
                    if not z.namelist():
                        raise zipfile.BadZipFile(tr("пустой архив"))
            except (zipfile.BadZipFile, OSError):
                result["broken"].append(path)
    return result


# --- Бэкапы: занятое место и очистка ---

def _live_inodes(folder: str) -> Set[tuple]:
    inodes = set()
    for name in os.listdir(folder) if os.path.isdir(folder) else []:
        try:
            st = os.stat(os.path.join(folder, name))
            inodes.add((st.st_dev, st.st_ino))
        except OSError:
            pass
    return inodes


def _dir_size(path: str, skip: Set[tuple], seen: Set[tuple]) -> int:
    """Размер папки без файлов, которые на самом деле общие (жёсткие ссылки на живые моды или друг на друга)."""
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                continue
            key = (st.st_dev, st.st_ino)
            if key in skip or key in seen:
                continue
            seen.add(key)
            total += st.st_size
    return total


def backups_usage(folder: str) -> Dict[str, int]:
    """Сколько места реально занимают бэкапы папки: операции и снимки."""
    live = _live_inodes(folder)
    seen: Set[tuple] = set()
    return {"transactions": _dir_size(transactions_root(folder), live, seen),
            "snapshots": _dir_size(snapshots_root(folder), live, seen)}


def _transaction_dirs(folder: str) -> List[str]:
    root = transactions_root(folder)
    return sorted((os.path.join(root, n) for n in os.listdir(root)), reverse=True) if os.path.isdir(root) else []


def prune_backups(folder: str, max_age_days: Optional[int] = None, max_bytes: Optional[int] = None,
                  keep_latest: int = 1) -> int:
    """
    Удаляет старые бэкапы операций (и автоматические снимки) по возрасту и общему размеру.
    Последняя операция и снимки, созданные вручную, не удаляются. Возвращает число удалённых папок.
    """
    removed = 0
    now = datetime.datetime.now()
    transactions = _transaction_dirs(folder)
    auto_snapshots = [s["dir"] for s in list_snapshots(folder) if s.get("reason") != "manual"]
    if max_age_days:
        threshold = (now - datetime.timedelta(days=max_age_days)).timestamp()
        for path in transactions[keep_latest:] + auto_snapshots[1:]:
            if os.path.getmtime(path) < threshold:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
    if max_bytes:
        # Сначала самые старые: операции и снимки вперемешку по дате
        candidates = [p for p in _transaction_dirs(folder)[keep_latest:] if os.path.isdir(p)]
        candidates += [s["dir"] for s in list_snapshots(folder) if s.get("reason") != "manual"][1:]
        candidates.sort(key=os.path.getmtime)
        while candidates and sum(backups_usage(folder).values()) > max_bytes:
            shutil.rmtree(candidates.pop(0), ignore_errors=True)
            removed += 1
    if removed:
        logging.info(f"Очистка бэкапов {folder}: удалено {removed}.")
    return removed


# --- Клонирование сборки ---

CLONE_ITEMS = ("mods", "config", "shaderpacks", "resourcepacks", "options.txt", "servers.dat")


def clone_game_dir(source: str, target: str, include_saves: bool = False) -> List[str]:
    """Копирует моды, настройки, шейдеры и ресурспаки (и, по желанию, миры) в новую папку игры."""
    os.makedirs(target, exist_ok=True)
    copied = []
    items = CLONE_ITEMS + (("saves",) if include_saves else ())
    ignore = shutil.ignore_patterns(BACKUP_DIR_NAME, "*.part")
    for item in items:
        src = os.path.join(source, item)
        dst = os.path.join(target, item)
        if os.path.isdir(src):
            shutil.copytree(src, dst, ignore=ignore, dirs_exist_ok=True)
            copied.append(item)
        elif os.path.isfile(src):
            shutil.copy2(src, dst)
            copied.append(item)
    return copied


# --- Сравнение сборок ---

def _compare_key(entry: Dict) -> str:
    return entry.get("project_id") or enabled_name(os.path.basename(entry.get("filepath") or "")).lower()


def compare_entries(left: Iterable[Dict], right: Iterable[Dict]) -> Dict[str, List]:
    """only_left / only_right — моды только в одной сборке; different — (мод слева, мод справа) с разными версиями."""
    a = {_compare_key(e): e for e in left if e.get("filepath")}
    b = {_compare_key(e): e for e in right if e.get("filepath")}

    def version(e):
        return e.get("current_version") or os.path.basename(e.get("filepath") or "")

    def name(e):
        return str(e.get("name") or "").lower()

    return {
        "only_left": sorted((a[k] for k in a.keys() - b.keys()), key=name),
        "only_right": sorted((b[k] for k in b.keys() - a.keys()), key=name),
        "different": sorted(((a[k], b[k]) for k in a.keys() & b.keys() if version(a[k]) != version(b[k])),
                            key=lambda pair: name(pair[0])),
        "same": [a[k] for k in a.keys() & b.keys() if version(a[k]) == version(b[k])],
    }


# --- Наборы модов ---

def preset_key(entry: Dict) -> str:
    """Проект Modrinth (переживает обновление мода) или имя файла для модов не с Modrinth."""
    return entry.get("project_id") or enabled_name(os.path.basename(entry.get("filepath") or "")).lower()


def preset_plan(entries: Iterable[Dict], enabled_keys: Iterable[str]) -> Dict[str, List[Dict]]:
    """Что включить и что выключить, чтобы включёнными остались ровно моды набора."""
    wanted = set(enabled_keys)
    plan = {"enable": [], "disable": []}
    for entry in entries:
        if not entry.get("filepath"):
            continue
        should_enable = preset_key(entry) in wanted
        if should_enable and entry.get("disabled"):
            plan["enable"].append(entry)
        elif not should_enable and not entry.get("disabled"):
            plan["disable"].append(entry)
    return plan


def enabled_keys(entries: Iterable[Dict]) -> List[str]:
    return sorted({preset_key(e) for e in entries if e.get("filepath") and not is_disabled(e["filepath"])})
