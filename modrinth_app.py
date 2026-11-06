"""
Modrinth App (без Qt): новая сборка (профиль) прямо в приложении.

Профиль Modrinth App — папка profiles/<имя> (она же папка игры) и строка в таблице profiles базы app.db.
Схема базы меняется от версии к версии, поэтому столбцы читаются из самой базы: известные заполняются по смыслу,
а незнакомые обязательные берутся из любого уже существующего профиля. Профиль записывается как «не установлен»:
при первом запуске приложение само скачает Minecraft и загрузчик. Пока приложение открыто, базу не трогаем —
оно держит профили в памяти и затёрло бы запись. Перед записью сохраняется копия базы.
"""
import datetime
import logging
import os
import shutil
import sqlite3
import time
from typing import Dict, List, Optional

from i18n import tr

PROCESSES = ("modrinth app.exe", "modrinth-app.exe", "theseus_gui.exe")
DB_NAME = "app.db"
KEEP_BACKUPS = 3


def roots() -> List[str]:
    from launchers import _appdata
    appdata = _appdata()
    return [os.path.join(appdata, name) for name in ("ModrinthApp", "com.modrinth.theseus")]


def find_root() -> Optional[str]:
    """Папка данных Modrinth App (в ней app.db и profiles) или None, если приложения нет."""
    return next((r for r in roots() if os.path.isfile(os.path.join(r, DB_NAME))), None)


def running() -> bool:
    from game_runner import processes_running
    return processes_running(PROCESSES)


def loader_version_for_app(loader: Optional[str], game_version: str, loader_version: Optional[str]) -> Optional[str]:
    """Forge в Modrinth App записывается с версией игры: «1.20.1-47.2.0»."""
    if loader == "forge" and loader_version and not loader_version.startswith(f"{game_version}-"):
        return f"{game_version}-{loader_version}"
    return loader_version


def _columns(conn) -> List[tuple]:
    """(имя, NOT NULL, есть значение по умолчанию) столбцов таблицы profiles."""
    rows = conn.execute("PRAGMA table_info(profiles)").fetchall()
    if not rows:
        raise ValueError(tr("В базе Modrinth App нет таблицы профилей — версия приложения не поддерживается."))
    return [(row[1], bool(row[3]), row[4] is not None) for row in rows]


def _values(conn, path: str, name: str, game_version: str, loader: Optional[str],
            loader_version: Optional[str]) -> Dict:
    now = int(time.time())
    known = {
        "path": path, "install_stage": "not_installed", "name": name, "icon_path": None,
        "game_version": game_version, "mod_loader": loader or "vanilla",
        "mod_loader_version": loader_version_for_app(loader, game_version, loader_version),
        "groups": "[]", "linked_project_id": None, "linked_version_id": None, "locked": None,
        "created": now, "modified": now, "last_played": None,
        "submitted_time_played": 0, "recent_time_played": 0,
        "override_java_path": None, "override_extra_launch_args": "[]", "override_custom_env_vars": "[]",
    }
    columns = _columns(conn)
    template = None
    values = {}
    for column, not_null, has_default in columns:
        if column in known:
            values[column] = known[column]
        elif not_null and not has_default:
            if template is None:  # Незнакомый обязательный столбец — как у существующего профиля
                conn.row_factory = sqlite3.Row
                template = conn.execute("SELECT * FROM profiles LIMIT 1").fetchone()
                conn.row_factory = None
                if template is None:
                    raise ValueError(tr("Незнакомая версия Modrinth App: создайте в ней любую сборку и повторите."))
            values[column] = template[column]
    return values


def check(root: str):
    """Можно ли сейчас добавить профиль. ValueError — почему нет."""
    if running():
        raise ValueError(tr("Закройте Modrinth App — пока оно открыто, новую сборку в него не добавить."))
    conn = sqlite3.connect(os.path.join(root, DB_NAME), timeout=5)
    try:
        _values(conn, "_", "_", "1.21.1", "fabric", "0")
    finally:
        conn.close()


def _backup(root: str) -> str:
    db = os.path.join(root, DB_NAME)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = f"{db}.modupdater-{stamp}.bak"
    shutil.copy2(db, backup)
    old = sorted(n for n in os.listdir(root) if n.startswith(DB_NAME + ".modupdater-") and n.endswith(".bak"))
    for name in old[:-KEEP_BACKUPS]:
        try:
            os.remove(os.path.join(root, name))
        except OSError:
            pass
    return backup


def add_profile(root: str, game_dir: str, name: str, game_version: str, loader: Optional[str],
                loader_version: Optional[str]) -> str:
    """Записывает профиль для папки profiles/<имя>. Возвращает путь копии базы."""
    check(root)
    path = os.path.basename(os.path.normpath(game_dir))
    backup = _backup(root)
    conn = sqlite3.connect(os.path.join(root, DB_NAME), timeout=5)
    try:
        values = _values(conn, path, name, game_version, loader, loader_version)
        columns = ", ".join(f'"{c}"' for c in values)
        conn.execute(f"INSERT INTO profiles ({columns}) VALUES ({', '.join('?' for _ in values)})",
                     list(values.values()))
        conn.commit()
    finally:
        conn.close()
    logging.info(tr("Modrinth App: добавлена сборка «{0}»", name))
    return backup


def remove_profile(root: str, game_dir: str):
    """Убирает профиль из базы (папку удаляет вызывающий)."""
    check(root)
    path = os.path.basename(os.path.normpath(game_dir))
    _backup(root)
    conn = sqlite3.connect(os.path.join(root, DB_NAME), timeout=5)
    try:
        conn.execute("DELETE FROM profiles WHERE path = ?", (path,))
        conn.commit()
    finally:
        conn.close()
