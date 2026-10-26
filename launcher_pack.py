"""
Версия Minecraft и загрузчика в самой сборке Prism Launcher / MultiMC (mmc-pack.json, рядом с папкой игры).

Раньше программа просила сменить их в лаунчере вручную; теперь меняет сама:
  * set_loader_version — новая версия загрузчика;
  * set_game_version — новая версия игры: Minecraft, intermediary (Fabric/Quilt), загрузчик и LWJGL
    по требованиям из справочника Prism (meta.prismlauncher.org) — так же, как это делает сам лаунчер.
Перед записью делается копия файла (*.modupdater-<время>.bak); restore() возвращает её.

LauncherTarget, change_versions и undo — общий вход для окна: то же самое для Prism/MultiMC и для установки
официального Minecraft Launcher (official_launcher.py).
"""
import datetime
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from i18n import tr

PACK_NAME = "mmc-pack.json"
META = "https://meta.prismlauncher.org/v1"
MINECRAFT = "net.minecraft"
INTERMEDIARY = "net.fabricmc.intermediary"
LOADER_UIDS = {"fabric": "net.fabricmc.fabric-loader", "quilt": "org.quiltmc.quilt-loader",
               "forge": "net.minecraftforge", "neoforge": "net.neoforged"}
LAUNCHER_PROCESSES = ("prismlauncher.exe", "multimc.exe")


def pack_file(game_dir: str) -> Optional[str]:
    """mmc-pack.json сборки: в папке экземпляра (родитель папки игры) или в самой папке."""
    if not game_dir:
        return None
    folder = os.path.normpath(game_dir)
    for candidate in (os.path.join(os.path.dirname(folder), PACK_NAME), os.path.join(folder, PACK_NAME)):
        if os.path.isfile(candidate):
            return candidate
    return None


def read(path: str) -> Dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data.get("components"), list):
        raise ValueError(tr("Неизвестный формат файла сборки лаунчера: {0}", path))
    return data


def versions(path: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """(версия Minecraft, загрузчик, версия загрузчика) по файлу сборки."""
    components = {c.get("uid"): c for c in read(path)["components"]}
    game = (components.get(MINECRAFT) or {}).get("version")
    for loader, uid in LOADER_UIDS.items():
        if uid in components:
            return game, loader, components[uid].get("version")
    return game, None, None


def _component(data: Dict, uid: str) -> Optional[Dict]:
    return next((c for c in data["components"] if c.get("uid") == uid), None)


def _set(component: Dict, version: str):
    component["version"] = version
    if "cachedVersion" in component:
        component["cachedVersion"] = version


def _write(path: str, data: Dict) -> str:
    """Запись с копией прежнего файла. Возвращает путь копии."""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{path}.modupdater-{stamp}.bak"
    shutil.copy2(path, backup)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    os.replace(tmp, path)
    return backup


def restore(path: str, backup: str):
    """Возвращает файл сборки из копии (кнопка «Отменить»)."""
    shutil.copy2(backup, path)


def set_loader_version(path: str, loader: str, version: str) -> str:
    data = read(path)
    component = _component(data, LOADER_UIDS.get(loader, ""))
    if component is None:
        raise ValueError(tr("В сборке лаунчера нет загрузчика {0}", loader))
    _set(component, version)
    component.pop("cachedRequires", None)  # Лаунчер пересчитает требования новой версии сам
    backup = _write(path, data)
    logging.info(tr("Сборка лаунчера: {0} {1}", loader, version))
    return backup


def fetch_requires(uid: str, version: str, session=None) -> List[Dict]:
    """Требования версии компонента из справочника Prism: [{"uid", "suggests"|"equals"}]."""
    from mod_updater_logic import SESSION
    response = (session or SESSION).get(f"{META}/{uid}/{version}.json", timeout=20)
    response.raise_for_status()
    return list(response.json().get("requires") or [])


def set_game_version(path: str, game_version: str, loader: Optional[str], loader_version: Optional[str],
                     session=None) -> str:
    """Новая версия Minecraft: сам Minecraft, его библиотеки (LWJGL), intermediary и загрузчик."""
    data = read(path)
    minecraft = _component(data, MINECRAFT)
    if minecraft is None:
        raise ValueError(tr("В сборке лаунчера нет Minecraft"))
    requires = fetch_requires(MINECRAFT, game_version, session)  # Заодно проверяет, что такая версия есть
    _set(minecraft, game_version)
    minecraft["cachedRequires"] = requires
    for need in requires:  # LWJGL нужной версии
        version = need.get("equals") or need.get("suggests")
        if not version:
            continue
        component = _component(data, need["uid"])
        if component is None:
            data["components"].insert(0, {"uid": need["uid"], "version": version, "dependencyOnly": True})
        else:
            _set(component, version)
    intermediary = _component(data, INTERMEDIARY)
    if intermediary is not None:  # Fabric и Quilt: intermediary всегда совпадает с версией игры
        _set(intermediary, game_version)
    if loader and loader_version:
        component = _component(data, LOADER_UIDS.get(loader, ""))
        if component is not None:
            _set(component, loader_version)
            component.pop("cachedRequires", None)
    backup = _write(path, data)
    logging.info(tr("Сборка лаунчера: Minecraft {0}, {1} {2}", game_version, loader or "—", loader_version or ""))
    return backup


@dataclass
class LauncherTarget:
    kind: str                           # "pack" — Prism/MultiMC, "official" — Minecraft Launcher
    path: str                           # mmc-pack.json или папка .minecraft
    key: str = ""                       # Установка официального лаунчера
    game_version: Optional[str] = None  # Версия игры сейчас


def change_versions(target: LauncherTarget, game_version: str, loader: Optional[str], loader_version: Optional[str],
                    session=None) -> str:
    """Новая версия игры и/или загрузчика в лаунчере (нужна сеть). Возвращает копию для undo()."""
    if target.kind == "pack":
        if game_version != target.game_version:
            return set_game_version(target.path, game_version, loader, loader_version, session)
        return set_loader_version(target.path, loader, loader_version)
    import official_launcher
    if not loader_version:
        raise ValueError(tr("Не удалось узнать версию загрузчика {0} для Minecraft {1}", loader, game_version))
    vid = official_launcher.install_loader(target.path, loader, game_version, loader_version, session)
    return official_launcher.set_installation_version(target.path, target.key, vid)


def undo(target: LauncherTarget, backup: str):
    if target.kind == "pack":
        restore(target.path, backup)
    else:
        import official_launcher
        official_launcher.restore(target.path, backup)


def launcher_running() -> bool:
    """Открыт ли Prism Launcher / MultiMC: открытый лаунчер может вернуть старые версии, сохраняя сборку."""
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False
    return any(f'"{name}"' in out for name in LAUNCHER_PROCESSES)
