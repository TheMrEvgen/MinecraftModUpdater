"""
Файлы сборки CurseForge, которые автор разрешает скачивать только с сайта (без Qt).

CurseForge не отдаёт программам ссылку на такие файлы — обходить это нельзя. Но:
  * тот же самый файл (совпала контрольная сумма SHA-1) часто лежит и на Modrinth, где скачивание разрешено, —
    его программа качает оттуда сама (find_substitutes → exact);
  * если на Modrinth есть только похожий файл (тот же мод для той же версии игры и загрузчика, но другая сборка),
    пользователь решает сам: скачать его с Modrinth или скачать оригинал с сайта CurseForge (similar);
  * скачанное браузером программа находит в папке «Загрузки» — по имени или, если браузер переименовал файл,
    по размеру и контрольной сумме — и кладёт в сборку (match_downloads / place).
Замена с Modrinth запоминается в состоянии сборки (substitutes): проверка сборки считает такой файл на месте.
"""
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from i18n import tr

PARTIAL = (".crdownload", ".part", ".tmp", ".download", ".opdownload")  # Браузер ещё качает
# Папка сборки → тип проекта Modrinth и загрузчики, по которым ищутся версии
PROJECT_TYPES = {"mods": "mod", "resourcepacks": "resourcepack", "shaderpacks": "shader", "datapacks": "datapack"}


@dataclass
class Substitute:
    exact: bool                       # Тот же файл байт в байт (совпала SHA-1) — качаем сами
    url: str
    filename: str
    hashes: Dict[str, str] = field(default_factory=dict)
    size: int = 0
    title: str = ""                   # Название проекта на Modrinth
    version: str = ""                 # Номер версии на Modrinth
    page_url: str = ""


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", (text or "").lower())


def _session(session):
    from mod_updater_logic import SESSION
    return session or SESSION


def _exact(items, session) -> Dict[str, Substitute]:
    from mod_updater_logic import MODRINTH_API_BASE
    by_sha = {f.hashes["sha1"].lower(): f for f in items if (f.hashes or {}).get("sha1")}
    if not by_sha:
        return {}
    response = _session(session).post(f"{MODRINTH_API_BASE}/version_files",
                                      json={"hashes": list(by_sha), "algorithm": "sha1"}, timeout=20)
    response.raise_for_status()
    result = {}
    for sha, version in (response.json() or {}).items():
        item = by_sha.get(sha.lower())
        file = next((f for f in version.get("files") or []
                     if ((f.get("hashes") or {}).get("sha1") or "").lower() == sha.lower()), None)
        if item and file and file.get("url"):
            result[item.path] = Substitute(True, file["url"], file.get("filename") or "", dict(file.get("hashes") or {}),
                                           int(file.get("size") or 0), version.get("name") or "",
                                           version.get("version_number") or "",
                                           f"https://modrinth.com/project/{version.get('project_id')}")
    return result


def _similar(item, game_version: str, loader: Optional[str], session) -> Optional[Substitute]:
    """Тот же проект на Modrinth (по названию) с версией для этой игры и загрузчика. Только точное совпадение имени."""
    from mod_updater_logic import MODRINTH_API_BASE
    folder = item.path.split("/", 1)[0]
    project_type = PROJECT_TYPES.get(folder)
    if not project_type or not item.name or not game_version:
        return None
    facets = [[f"project_type:{project_type}"], [f"versions:{game_version}"]]
    response = _session(session).get(f"{MODRINTH_API_BASE}/search", timeout=20,
                                     params={"query": item.name, "facets": json.dumps(facets), "limit": 5})
    response.raise_for_status()
    wanted = _norm(item.name)
    hit = next((h for h in response.json().get("hits") or [] if _norm(h.get("title")) == wanted), None)
    if not hit:
        return None
    loaders = {"mod": [loader] if loader else [], "resourcepack": ["minecraft"], "datapack": ["datapack"]}.get(
        project_type, [])
    params = {"game_versions": json.dumps([game_version])}
    if loaders:
        params["loaders"] = json.dumps(loaders)
    response = _session(session).get(f"{MODRINTH_API_BASE}/project/{hit['project_id']}/version", params=params,
                                     timeout=20)
    response.raise_for_status()
    versions = response.json() or []
    if not versions:
        return None
    version = versions[0]
    file = next((f for f in version.get("files") or [] if f.get("primary")), (version.get("files") or [None])[0])
    if not file or not file.get("url"):
        return None
    return Substitute(False, file["url"], file.get("filename") or "", dict(file.get("hashes") or {}),
                      int(file.get("size") or 0), hit.get("title") or "", version.get("version_number") or "",
                      f"https://modrinth.com/{project_type}/{hit.get('slug') or hit['project_id']}")


def find_substitutes(items: List, game_version: str, loader: Optional[str], session=None) -> Dict[str, Substitute]:
    """Замены на Modrinth для файлов «только с сайта»: {путь в сборке: Substitute}. Ошибки сети — без замены."""
    if not items:
        return {}
    try:
        result = _exact(items, session)
    except Exception as e:
        logging.warning(tr("Modrinth: не удалось поискать файлы сборки по контрольной сумме: {0}", e))
        result = {}
    for item in items:
        if item.path in result:
            continue
        try:
            similar = _similar(item, game_version, loader, session)
        except Exception as e:
            logging.debug(f"Modrinth: похожий файл для {item.path} не найден: {e}")
            similar = None
        if similar:
            result[item.path] = similar
    return result


def resolve_manual(plan, session=None) -> int:
    """
    Файлы «только с сайта», которые точно такими же лежат на Modrinth, получают ссылку оттуда и качаются как
    обычные. Похожие — в plan.substitutes (выбирает пользователь). Возвращает, сколько файлов стало можно скачать.
    """
    manual = [f for f in plan.files if not f.url]
    found = find_substitutes(manual, plan.game_version or "", plan.loader, session)
    count = 0
    for item in manual:
        sub = found.get(item.path)
        if sub and sub.exact:
            item.url = sub.url
            item.hashes = {**sub.hashes, **(item.hashes or {})}  # SHA-1 та же; SHA-512 от Modrinth — строже
            item.size = item.size or sub.size
            count += 1
    plan.substitutes = {path: sub for path, sub in found.items() if not sub.exact}
    plan.from_modrinth = count
    if count:
        logging.info(tr("Сборка: файлов «только с сайта» нашлось на Modrinth (те же самые) — {0}", count))
    return count


# --- Папка «Загрузки» ---

def _hash_ok(path: str, hashes: Dict[str, str]) -> bool:
    from modpack_install import _check_hash
    return _check_hash(path, hashes)


def match_downloads(items: List, folder: str, since: float = 0.0) -> Dict[str, str]:
    """
    Какие из файлов items лежат в folder: {путь в сборке: файл}. По имени (с проверкой контрольной суммы, если
    она известна) или, если браузер переименовал файл («mod (1).jar»), по размеру и контрольной сумме среди
    файлов, появившихся после since.
    """
    if not items or not os.path.isdir(folder):
        return {}
    entries = []
    for name in os.listdir(folder):
        full = os.path.join(folder, name)
        if name.lower().endswith(PARTIAL) or not os.path.isfile(full):
            continue
        try:
            stat = os.stat(full)
        except OSError:
            continue
        entries.append((name, full, stat.st_size, stat.st_mtime))
    result = {}
    for item in items:
        wanted = os.path.basename(item.path)
        by_name = next((e for e in entries if e[0] == wanted), None)
        if by_name and _hash_ok(by_name[1], item.hashes):
            result[item.path] = by_name[1]
            continue
        if not item.hashes or not item.size:
            continue  # Без размера и суммы переименованный файл не узнать
        for name, full, size, mtime in entries:
            if size == item.size and mtime >= since and _hash_ok(full, item.hashes):
                result[item.path] = full
                break
    return result


def place(item, source: str, game_dir: str) -> str:
    """Кладёт скачанный файл в сборку (копией — в «Загрузках» он остаётся). Возвращает путь в сборке."""
    from modpack_install import _abs, _on_disk
    relative = _on_disk(game_dir, item.path) or item.path  # Выключенный мод остаётся выключенным
    destination = _abs(game_dir, relative)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    shutil.copy2(source, destination + ".part")
    os.replace(destination + ".part", destination)
    return destination


# --- Замены в состоянии сборки ---

def substitutes_of(game_dir: str) -> Dict[str, str]:
    """{путь файла сборки: путь замены с Modrinth} поставленной сборки."""
    from modpack_install import load_state
    return dict((load_state(game_dir) or {}).get("substitutes") or {})


def record_substitute(game_dir: str, path: str, relative: str):
    """Запоминает, что файл сборки path заменён файлом relative (похожим с Modrinth)."""
    from modpack_install import load_state, state_path
    state = load_state(game_dir)
    if not state:
        return
    state.setdefault("substitutes", {})[path] = relative
    with open(state_path(game_dir), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def download_similar(item, sub: Substitute, game_dir: str, session=None) -> str:
    """Качает похожий файл с Modrinth рядом с тем местом, где должен лежать оригинал. Возвращает путь в сборке."""
    from modpack_install import PackFile, _abs, download_files
    folder = item.path.rsplit("/", 1)[0] if "/" in item.path else ""
    relative = f"{folder}/{sub.filename}" if folder else sub.filename
    destination = _abs(game_dir, relative)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    part = destination + ".part"
    errors = download_files([(PackFile(relative, sub.url, sub.hashes, sub.size, name=sub.title or item.name), part)],
                            lambda *a: None, lambda: False, _session(session))
    if errors:
        raise ValueError(next(iter(errors.values())))
    os.replace(part, destination)
    record_substitute(game_dir, item.path, relative)
    logging.info(tr("Сборка: вместо {0} поставлен похожий файл с Modrinth {1}", item.path, relative))
    return relative


def still_missing(items: List, game_dir: str) -> List:
    """Файлы «только с сайта», которых всё ещё нет в сборке (ни оригинала, ни замены с Modrinth)."""
    from modpack_install import _abs, _on_disk
    subs = substitutes_of(game_dir)
    return [f for f in items if not _on_disk(game_dir, f.path)
            and not (subs.get(f.path) and os.path.isfile(_abs(game_dir, subs[f.path])))]


def downloads_since() -> float:
    """Отметка времени для поиска переименованных файлов: всё, что скачано с этой минуты (с запасом)."""
    return time.time() - 60
