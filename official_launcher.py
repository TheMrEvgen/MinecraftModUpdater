"""
Официальный Minecraft Launcher (обычный и из Microsoft Store): установки и загрузчики Fabric/Quilt (без Qt).

Установки лаунчера («Установки» в его окне) записаны в .minecraft/launcher_profiles.json: название, версия
(lastVersionId) и, если задана, своя папка игры (gameDir). Сами версии всегда лежат в .minecraft/versions,
даже когда папка игры у установки своя.

Fabric и Quilt ставятся так же, как это делают их установщики: файл версии из их справочника кладётся в
versions/<id>/<id>.json (рядом — пустой <id>.jar, без него лаунчер считает версию сломанной), а версия
установки меняется в launcher_profiles.json. Перед каждой правкой launcher_profiles.json сохраняется копия —
изменение можно отменить. Forge и NeoForge ставятся только своими установщиками.
"""
import datetime
import json
import logging
import os
import shutil
import sys
import uuid
from typing import Dict, List, Optional

from i18n import tr

PROFILES_FILE = "launcher_profiles.json"
LOADER_META = {
    "fabric": ("https://meta.fabricmc.net/v2", "fabric-loader-{loader}-{game}"),
    "quilt": ("https://meta.quiltmc.org/v3", "quilt-loader-{loader}-{game}"),
}
LAUNCHER_PROCESSES = ("minecraftlauncher.exe", "minecraft.exe")
KEEP_BACKUPS = 5


def minecraft_dir() -> str:
    if sys.platform == "win32":
        return os.path.join(os.getenv("APPDATA") or os.path.expanduser("~"), ".minecraft")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/minecraft")
    return os.path.expanduser("~/.minecraft")


def profiles_path(mc_dir: str) -> str:
    return os.path.join(mc_dir, PROFILES_FILE)


def read_profiles(mc_dir: str) -> Dict:
    with open(profiles_path(mc_dir), encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
        raise ValueError(tr("Неизвестный формат файла {0}", profiles_path(mc_dir)))
    return data


def installations(mc_dir: str) -> List[Dict]:
    """Установки лаунчера: {"id", "name", "version_id", "game_dir", "last_used"}; недавние — первыми."""
    try:
        profiles = read_profiles(mc_dir)["profiles"]
    except (OSError, ValueError):
        return []
    result = []
    for key, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        version_id = str(profile.get("lastVersionId") or "")
        if profile.get("type") in ("latest-release", "latest-snapshot") or version_id.startswith("latest-"):
            continue  # «Последний выпуск» — всегда ванильная игра
        result.append({"id": key, "name": profile.get("name") or version_id, "version_id": version_id,
                       "game_dir": profile.get("gameDir") or mc_dir, "last_used": profile.get("lastUsed") or ""})
    result.sort(key=lambda i: i["last_used"], reverse=True)
    return result


def version_id(loader: str, game_version: str, loader_version: str) -> str:
    return LOADER_META[loader][1].format(loader=loader_version, game=game_version)


def can_install(loader: Optional[str]) -> bool:
    return loader in LOADER_META


def install_loader(mc_dir: str, loader: str, game_version: str, loader_version: str, session=None) -> str:
    """Ставит Fabric/Quilt нужной версии в versions/ (если ещё не стоит). Возвращает id версии для лаунчера."""
    from mod_updater_logic import SESSION
    if loader not in LOADER_META:
        raise ValueError(tr("{0} ставится только своим установщиком — поставьте его вручную.", loader))
    base, _ = LOADER_META[loader]
    vid = version_id(loader, game_version, loader_version)
    folder = os.path.join(mc_dir, "versions", vid)
    json_path = os.path.join(folder, f"{vid}.json")
    if not os.path.isfile(json_path):
        response = (session or SESSION).get(f"{base}/versions/loader/{game_version}/{loader_version}/profile/json",
                                            timeout=30)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not data.get("id"):
            raise ValueError(tr("Справочник {0} не знает версию {1} для Minecraft {2}", loader, loader_version,
                                game_version))
        data["id"] = vid
        os.makedirs(folder, exist_ok=True)
        with open(json_path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(json_path + ".tmp", json_path)
        logging.info(tr("Minecraft Launcher: установлен {0}", vid))
    jar = os.path.join(folder, f"{vid}.jar")
    if not os.path.exists(jar):
        open(jar, "wb").close()
    return vid


def _write(mc_dir: str, data: Dict) -> str:
    """Запись launcher_profiles.json с копией прежнего файла. Возвращает путь копии."""
    path = profiles_path(mc_dir)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = f"{path}.modupdater-{stamp}.bak"
    shutil.copy2(path, backup)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    old = sorted(n for n in os.listdir(mc_dir) if n.startswith(PROFILES_FILE + ".modupdater-") and n.endswith(".bak"))
    for name in old[:-KEEP_BACKUPS]:
        try:
            os.remove(os.path.join(mc_dir, name))
        except OSError:
            pass
    return backup


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def add_installation(mc_dir: str, name: str, vid: str, game_dir: str) -> tuple:
    """Новая установка со своей папкой игры. Возвращает (id установки, копия файла)."""
    if not os.path.isfile(profiles_path(mc_dir)):  # Лаунчер ещё ни разу не запускали
        os.makedirs(mc_dir, exist_ok=True)
        with open(profiles_path(mc_dir), "w", encoding="utf-8") as f:
            json.dump({"profiles": {}, "settings": {}, "version": 3}, f)
    data = read_profiles(mc_dir)
    key = uuid.uuid4().hex
    data["profiles"][key] = {"name": name, "type": "custom", "created": _now(), "lastUsed": _now(), "icon": "Furnace",
                             "lastVersionId": vid, "gameDir": os.path.abspath(game_dir)}
    return key, _write(mc_dir, data)


def set_installation_version(mc_dir: str, key: str, vid: str) -> str:
    """Меняет версию установки. Возвращает путь копии файла (для отмены)."""
    data = read_profiles(mc_dir)
    profile = data["profiles"].get(key)
    if not isinstance(profile, dict):
        raise ValueError(tr("Установка не найдена в Minecraft Launcher — возможно, её удалили."))
    profile["lastVersionId"] = vid
    return _write(mc_dir, data)


def mark_last_used(mc_dir: str, key: str):
    """Делает установку последней запущенной: лаунчер откроется сразу с ней. Без копии — это не изменение версий."""
    try:
        data = read_profiles(mc_dir)
        if key in data["profiles"]:
            data["profiles"][key]["lastUsed"] = _now()
            tmp = profiles_path(mc_dir) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, profiles_path(mc_dir))
    except (OSError, ValueError) as e:
        logging.warning(tr("Minecraft Launcher: не удалось выбрать установку: {0}", e))


def restore(mc_dir: str, backup: str):
    shutil.copy2(backup, profiles_path(mc_dir))


def launcher_running() -> bool:
    """Открыт ли Minecraft Launcher: при закрытии он сохраняет свои установки и может затереть наши изменения."""
    from game_runner import processes_running
    return processes_running(LAUNCHER_PROCESSES)
