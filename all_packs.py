"""
Все сборки сразу (без Qt): поиск мода по всем сборкам и место на диске.

Одинаковые файлы модов в разных сборках можно объединить жёсткими ссылками — только по команде пользователя.
Файлы остаются на своих местах, но на диске хранятся один раз. Программа обновляет мод, записывая новый файл
(а старый уходит в бэкап), поэтому обновление в одной сборке другие не затрагивает.
"""
import logging
import os
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

from content import MODS
from i18n import tr


def collect(profiles: Dict[str, Dict], cache_file: Optional[str]) -> List[Dict]:
    """Моды всех сборок по локальному чтению папок (названия — из кэша прошлых проверок, без сети)."""
    from scanner import scan_local
    rows: List[Dict] = []
    seen_dirs = set()
    for name, profile in profiles.items():
        mods_dir = MODS.path(profile.get("mods_path") or "")
        key = os.path.normcase(os.path.abspath(mods_dir)) if mods_dir else ""
        if not mods_dir or not os.path.isdir(mods_dir) or key in seen_dirs:
            continue  # Две сборки с одной папкой — считаем один раз
        seen_dirs.add(key)
        try:
            entries = scan_local(mods_dir, cache_file, MODS)
        except Exception as e:  # Битая папка одной сборки не мешает остальным
            logging.warning(tr("Не удалось прочитать моды «{0}»: {1}", name, e))
            continue
        for entry in entries:
            for item in [entry] + list(entry.get("dependencies") or []):
                if item.get("filepath") and os.path.isfile(item["filepath"]):
                    rows.append({"pack": name, "name": item.get("name") or os.path.basename(item["filepath"]),
                                 "version": item.get("current_version") or "", "path": item["filepath"],
                                 "sha1": item.get("sha1"), "size": item.get("size") or os.path.getsize(item["filepath"]),
                                 "disabled": bool(item.get("disabled"))})
    return rows


def search(rows: List[Dict], query: str) -> List[Tuple[str, List[Dict]]]:
    """[(название мода, [строки по сборкам])] — по названию или имени файла; самые распространённые первыми."""
    words = (query or "").lower().split()
    groups: Dict[str, List[Dict]] = defaultdict(list)
    for row in rows:
        text = f"{row['name']} {os.path.basename(row['path'])}".lower()
        if all(word in text for word in words):
            groups[row["name"]].append(row)
    return sorted(groups.items(), key=lambda g: (-len({r["pack"] for r in g[1]}), g[0].lower()))


def _identity(path: str) -> Tuple[int, int]:
    info = os.stat(path)
    return info.st_dev, info.st_ino


def duplicate_groups(rows: Iterable[Dict]) -> List[List[Dict]]:
    """Одинаковые файлы (по SHA1) на одном диске, которые ещё не объединены — по группе на содержимое."""
    by_content: Dict[Tuple, List[Dict]] = defaultdict(list)
    for row in rows:
        if row.get("sha1"):
            try:
                device, inode = _identity(row["path"])
            except OSError:
                continue
            by_content[(row["sha1"], device)].append({**row, "inode": inode})
    return [group for group in by_content.values() if len({r["inode"] for r in group}) > 1]


def savings(groups: List[List[Dict]]) -> int:
    """Сколько байт освободит объединение: каждый лишний физический файл группы."""
    return sum(group[0]["size"] * (len({r["inode"] for r in group}) - 1) for group in groups)


def link_group(group: List[Dict]) -> Tuple[int, List[str]]:
    """Заменяет копии жёсткими ссылками на первый файл группы. (объединено, ошибки)."""
    source = group[0]["path"]
    linked, errors = 0, []
    for row in group[1:]:
        if row["inode"] == group[0]["inode"]:
            continue
        temp = row["path"] + ".modupdater-link"
        try:
            if os.path.exists(temp):
                os.remove(temp)
            os.link(source, temp)
            os.replace(temp, row["path"])  # Занятый файл (игра запущена) — ошибка, файл остаётся как был
            linked += 1
        except OSError as e:
            errors.append(f"{row['pack']}: {os.path.basename(row['path'])} — {e}")
            if os.path.exists(temp):
                try:
                    os.remove(temp)
                except OSError:
                    pass
    return linked, errors


def link_all(groups: List[List[Dict]]) -> Tuple[int, int, List[str]]:
    """Объединяет все группы. (объединено файлов, освобождено байт, ошибки)."""
    total, freed, errors = 0, 0, []
    for group in groups:
        linked, group_errors = link_group(group)
        total += linked
        freed += linked * group[0]["size"]
        errors += group_errors
    logging.info(tr("Объединено одинаковых файлов: {0}", total))
    return total, freed, errors
