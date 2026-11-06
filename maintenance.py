"""
Обслуживание сборки (без Qt): проверка целостности, место под бэкапы и их очистка,
клонирование и сравнение сборок, наборы модов (какие включены).
"""
import datetime
import logging
import os
import shutil
import sys
import zipfile
from dataclasses import dataclass
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
        logging.info(tr('Очистка бэкапов {0}: удалено {1}.', folder, removed))
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


# --- Перенос сборки в другую папку (другой лаунчер) ---

# Служебное лаунчера, а не сборки: версии игры, библиотеки, ресурсы, Java, логи, учётные записи. Не переносим —
# в новой папке это всё у своего лаунчера. modpacks — другие сборки Minecraft Launcher, их тоже не трогаем.
LAUNCHER_ITEMS = {"versions", "libraries", "assets", "runtime", "bin", "natives", "webcache", "webcache2", "logs",
                  "crash-reports", "debug", ".mixin.out", "modpacks", "treatment_tags.json", "usercache.json",
                  "usernamecache.json"}


def movable_items(source: str) -> List[str]:
    """Что из папки игры относится к сборке: моды, настройки, миры, ресурспаки, шейдеры и прочее."""
    if not os.path.isdir(source):
        return []
    return sorted(name for name in os.listdir(source)
                  if name.lower() not in LAUNCHER_ITEMS and not name.lower().startswith("launcher_"))


def folder_size(path: str) -> int:
    """Размер файла или папки со всем содержимым, байт."""
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _inside(path: str, parent: str) -> bool:
    path, parent = (os.path.normcase(os.path.realpath(p)) for p in (path, parent))
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def move_game_dir(source: str, target: str, keep_source: bool = False) -> List[str]:
    """
    Переносит сборку из одной папки игры в другую (например, в папку другого лаунчера). Совпадающие папки
    сливаются, одинаковые файлы в новой папке заменяются. keep_source — скопировать, ничего не удаляя в старой.
    Возвращает перенесённое. ValueError — папки вложены друг в друга так, что перенос невозможен.
    """
    if not os.path.isdir(source):
        raise ValueError(tr("Старой папки сборки нет: {0}", source))
    if _inside(target, source):
        raise ValueError(tr("Новая папка находится внутри старой — выберите папку вне её."))
    os.makedirs(target, exist_ok=True)
    if keep_source or os.stat(source).st_dev != os.stat(target).st_dev:  # Файлы копируются — нужно место
        need = sum(folder_size(os.path.join(source, n)) for n in movable_items(source))
        free = shutil.disk_usage(target).free
        if need > free:
            from view_model import format_size
            raise ValueError(tr("Не хватает места: сборке нужно {0}, а на диске свободно {1}.",
                                format_size(need), format_size(free)))
    moved = []
    for name in movable_items(source):
        src, dst = os.path.join(source, name), os.path.join(target, name)
        if _inside(target, src):
            continue  # Новая папка лежит внутри этого элемента (например, старая папка — .minecraft)
        if os.path.isdir(src):
            if keep_source or os.path.exists(dst):
                shutil.copytree(src, dst, dirs_exist_ok=True)
                if not keep_source:
                    shutil.rmtree(src)
            else:
                shutil.move(src, dst)
        else:
            if keep_source:
                shutil.copy2(src, dst)
            else:
                if os.path.isfile(dst):
                    os.remove(dst)  # shutil.move не заменяет файл на другом диске
                shutil.move(src, dst)
        moved.append(name)
    what = ", ".join(moved) or "—"
    if keep_source:
        logging.info(tr("Сборка {0} скопирована в {1}: {2}", source, target, what))
    else:
        logging.info(tr("Сборка {0} перенесена в {1}: {2}", source, target, what))
    return moved


# --- Удаление сборки с диска ---

def to_recycle_bin(path: str):
    """Удаляет файл или папку в Корзину Windows (можно восстановить). Не Windows — удаляет насовсем."""
    if not os.path.exists(path):
        return
    if sys.platform != "win32":
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
        return
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", ctypes.c_uint), ("pFrom", ctypes.c_wchar_p),
                    ("pTo", ctypes.c_wchar_p), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p)]

    fo_delete, flags = 3, 0x0040 | 0x0010 | 0x0004 | 0x0400  # ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI
    operation = SHFILEOPSTRUCTW(None, fo_delete, os.path.abspath(path) + "\0", None, flags, False, None, None)
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if code or operation.fAnyOperationsAborted or os.path.exists(path):
        raise OSError(tr("Не удалось удалить {0} (код {1}) — возможно, файлы открыты в игре или лаунчере.", path, code))


def _protected(path: str) -> bool:
    """Папки, которые нельзя удалять целиком: корень диска, профиль пользователя и его стандартные папки."""
    path = os.path.normcase(os.path.abspath(path))
    if os.path.dirname(path) == path:
        return True
    home = os.path.expanduser("~")
    roots = [home] + [os.path.join(home, n) for n in ("Desktop", "Documents", "Downloads", "AppData")]
    roots += [os.getenv(k) or "" for k in ("APPDATA", "LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)",
                                           "SystemRoot", "USERPROFILE")]
    return any(r and path == os.path.normcase(os.path.abspath(r)) for r in roots)


@dataclass
class Deletion:
    paths: List[str]                    # Что уходит в Корзину
    official_key: str = ""              # Установка Minecraft Launcher, которую убрать из его списка
    modrinth_root: str = ""             # Modrinth App: убрать профиль из его базы


def deletion_plan(profile: Dict) -> Deletion:
    """
    Что удалить вместе со сборкой. ValueError — удалять нельзя: общая папка лаунчера (в ней версии игры и другие
    сборки, например .minecraft) или системная папка; такую сборку можно только убрать из списка.
    """
    import official_launcher
    from game_runner import instance_dir
    game_dir = profile.get("mods_path") or ""
    if not game_dir or not os.path.isdir(game_dir):
        raise ValueError(tr("Папки сборки нет на диске — просто уберите её из списка."))
    source = profile.get("source") or "manual"
    if source in ("prism", "multimc"):
        instance = instance_dir(game_dir)
        if os.path.isfile(os.path.join(instance, "mmc-pack.json")) and not _protected(instance):
            return Deletion([instance])
    shared = any(os.path.exists(os.path.join(game_dir, n)) for n in ("versions", "libraries", "launcher_profiles.json"))
    if shared or _protected(game_dir):
        raise ValueError(tr("«{0}» — общая папка лаунчера (в ней версии игры и, возможно, другие сборки). Её нельзя "
                            "удалить целиком: уберите сборку из списка, а лишнее удалите вручную.", game_dir))
    deletion = Deletion([game_dir])
    if source == "official" and profile.get("launcher_id"):
        deletion.official_key = profile["launcher_id"]
    if source == "modrinth":
        import modrinth_app
        root = os.path.dirname(os.path.dirname(os.path.normpath(game_dir)))  # <данные>/profiles/<сборка>
        if os.path.isfile(os.path.join(root, modrinth_app.DB_NAME)):
            deletion.modrinth_root = root
    if source == "official" and not deletion.official_key:
        mc_dir = official_launcher.minecraft_dir()
        if os.path.normcase(os.path.abspath(game_dir)) == os.path.normcase(os.path.abspath(mc_dir)):
            raise ValueError(tr("Это папка .minecraft самого лаунчера — удалить её целиком нельзя."))
    return deletion


def delete_pack(deletion: Deletion):
    """Удаляет сборку по плану deletion_plan: сначала записи лаунчеров, потом папки — в Корзину."""
    if deletion.modrinth_root:
        import modrinth_app
        modrinth_app.check(deletion.modrinth_root)  # Приложение открыто — ничего не трогаем
    for path in deletion.paths:
        to_recycle_bin(path)
    if deletion.official_key:
        import official_launcher
        official_launcher.remove_installation(official_launcher.minecraft_dir(), deletion.official_key)
    if deletion.modrinth_root:
        import modrinth_app
        modrinth_app.remove_profile(deletion.modrinth_root, deletion.paths[0])
    logging.info(tr("Сборка удалена в Корзину: {0}", ", ".join(deletion.paths)))


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
