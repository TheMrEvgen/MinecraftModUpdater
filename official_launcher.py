"""
Официальный Minecraft Launcher (обычный и из Microsoft Store): установки и загрузчики Fabric/Quilt (без Qt).

Установки лаунчера («Установки» в его окне) записаны в .minecraft/launcher_profiles.json: название, версия
(lastVersionId) и, если задана, своя папка игры (gameDir). Сами версии всегда лежат в .minecraft/versions,
даже когда папка игры у установки своя.

Fabric и Quilt ставятся так же, как это делают их установщики: файл версии из их справочника кладётся в
versions/<id>/<id>.json (рядом — пустой <id>.jar, без него лаунчер считает версию сломанной), а версия
установки меняется в launcher_profiles.json. Перед каждой правкой launcher_profiles.json сохраняется копия —
изменение можно отменить.

Forge и NeoForge ставятся только своими установщиками: программа скачивает установщик с их сайта и запускает его
без окна (java -jar …-installer.jar --installClient <папка>) на подходящей Java, найденной на компьютере.
Так же загрузчик ставится и в любую папку игры, где лаунчер берёт версии из папки versions (Legacy Launcher и т.п.).
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


INSTALLER_LOADERS = ("forge", "neoforge")  # Ставятся своим установщиком (нужна Java)
INSTALLER_TIMEOUT = 20 * 60                # Установщик качает Minecraft и библиотеки — может идти долго


def version_id(loader: str, game_version: str, loader_version: str) -> str:
    if loader in INSTALLER_LOADERS:
        version = forge_version(game_version, loader_version) if loader == "forge" else loader_version
        return f"{game_version}-forge-{version}" if loader == "forge" else f"neoforge-{version}"
    return LOADER_META[loader][1].format(loader=loader_version, game=game_version)


def forge_version(game_version: str, loader_version: str) -> str:
    """«1.20.1-47.2.0» → «47.2.0»: так версия Forge записана в имени установщика."""
    prefix = f"{game_version}-"
    return loader_version[len(prefix):] if loader_version.startswith(prefix) else loader_version


def can_install(loader: Optional[str]) -> bool:
    return loader in LOADER_META or loader in INSTALLER_LOADERS


def ensure_profiles_file(mc_dir: str):
    """Пустой launcher_profiles.json: без него установщики Forge/NeoForge отказываются ставить."""
    if not os.path.isfile(profiles_path(mc_dir)):
        os.makedirs(mc_dir, exist_ok=True)
        with open(profiles_path(mc_dir), "w", encoding="utf-8") as f:
            json.dump({"profiles": {}, "settings": {}, "version": 3}, f)


def install_loader(mc_dir: str, loader: str, game_version: str, loader_version: str, session=None,
                   progress=lambda text: None) -> str:
    """Ставит загрузчик нужной версии в versions/ (если ещё не стоит). Возвращает id версии для лаунчера."""
    from mod_updater_logic import SESSION
    if loader in INSTALLER_LOADERS:
        return _install_with_installer(mc_dir, loader, game_version, loader_version, session, progress)
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


def _version_dirs(mc_dir: str) -> set:
    versions = os.path.join(mc_dir, "versions")
    return set(os.listdir(versions)) if os.path.isdir(versions) else set()


def _installed_id(mc_dir: str, expected: str, before: set, loader: str) -> Optional[str]:
    """id версии, которую поставил установщик: ожидаемый или новая папка в versions с именем загрузчика."""
    if os.path.isfile(os.path.join(mc_dir, "versions", expected, f"{expected}.json")):
        return expected
    new = sorted(n for n in _version_dirs(mc_dir) - before if loader in n.lower())
    return new[0] if new else None


def _install_with_installer(mc_dir: str, loader: str, game_version: str, loader_version: str, session=None,
                            progress=lambda text: None) -> str:
    """Forge / NeoForge: скачать установщик и запустить его без окна. Возвращает id установленной версии."""
    import subprocess
    import tempfile

    import java_check
    import server_pack
    expected = version_id(loader, game_version, loader_version)
    if _installed_id(mc_dir, expected, _version_dirs(mc_dir), loader) == expected:
        return expected  # Уже стоит
    need = java_check.required_java(game_version, loader) or (17, None)
    java = java_check.find_java(*need)
    if not java:
        raise ValueError(tr("Для установки {0} нужна Java {1} — её нет на компьютере. Запустите один раз Minecraft "
                            "Launcher (он скачает свою Java) или установите Java {1}, затем повторите.",
                            loader, need[0]))
    ensure_profiles_file(mc_dir)
    version = forge_version(game_version, loader_version) if loader == "forge" else loader_version
    url = server_pack.INSTALLERS[loader].format(g=game_version, v=version)
    before = _version_dirs(mc_dir)
    with tempfile.TemporaryDirectory(prefix="modupdater-loader-") as work:
        installer = os.path.join(work, f"{loader}-installer.jar")
        progress(tr("Скачиваем установщик {0} {1}…", loader, version))
        server_pack._download(url, installer, session)
        progress(tr("Ставим {0} {1} (установщик качает Minecraft и библиотеки)…", loader, version))
        output = ""
        for option in ("--installClient", "--install-client"):  # Старые и новые установщики называют ключ по-разному
            try:
                result = subprocess.run([java, "-jar", installer, option, os.path.abspath(mc_dir)], cwd=work,
                                        capture_output=True, text=True, errors="replace", timeout=INSTALLER_TIMEOUT,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except subprocess.TimeoutExpired:
                raise ValueError(tr("Установщик {0} не уложился в {1} минут.", loader, INSTALLER_TIMEOUT // 60))
            output = (result.stdout or "") + (result.stderr or "")
            if "not a recognized option" not in output and "UnrecognizedOption" not in output:
                break
    vid = _installed_id(mc_dir, expected, before, loader)
    if result.returncode != 0 or not vid:
        tail = "\n".join(output.strip().splitlines()[-6:])
        raise ValueError(tr("Установщик {0} завершился с ошибкой:\n{1}", loader, tail or result.returncode))
    logging.info(tr("Minecraft Launcher: установлен {0}", vid))
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
    ensure_profiles_file(mc_dir)  # Лаунчер ещё ни разу не запускали
    data = read_profiles(mc_dir)
    key = uuid.uuid4().hex
    data["profiles"][key] = {"name": name, "type": "custom", "created": _now(), "lastUsed": _now(), "icon": "Furnace",
                             "lastVersionId": vid, "gameDir": os.path.abspath(game_dir)}
    return key, _write(mc_dir, data)


def remove_installation(mc_dir: str, key: str) -> Optional[str]:
    """Убирает установку из списка лаунчера. Возвращает путь копии файла (None — установки уже нет)."""
    try:
        data = read_profiles(mc_dir)
    except (OSError, ValueError):
        return None
    if key not in data["profiles"]:
        return None
    del data["profiles"][key]
    return _write(mc_dir, data)


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
