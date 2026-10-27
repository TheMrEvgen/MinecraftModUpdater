"""
Сборка для сервера (без Qt): zip-архив, который можно загрузить на хостинг или распаковать и запустить.

  * mods/ — моды сборки без «только для клиента» (по client_side/server_side с Modrinth; выбор можно поправить);
  * config/ и defaultconfigs/ — настройки модов (серверным модам они нужны так же, как в игре);
  * запуск: Fabric — готовый серверный jar с meta.fabricmc.net и start.bat/start.sh; Forge и NeoForge —
    их установщик и install.bat/install.sh (ставит сервер, дальше — run.bat), память — в user_jvm_args.txt;
    Quilt — инструкция в README.
EULA Minecraft программа не принимает: это решение владельца сервера (eula=true в eula.txt после первого запуска).
"""
import logging
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from content import enabled_name
from i18n import tr

CONFIG_FOLDERS = ("config", "defaultconfigs")
FABRIC_META = "https://meta.fabricmc.net/v2"
INSTALLERS = {
    "neoforge": "https://maven.neoforged.net/releases/net/neoforged/neoforge/{v}/neoforge-{v}-installer.jar",
    "forge": "https://maven.minecraftforge.net/net/minecraftforge/forge/{g}-{v}/forge-{g}-{v}-installer.jar",
}


def is_client_only(entry: Dict) -> bool:
    """Мод не нужен (или вреден) на сервере: на Modrinth у него server_side = unsupported."""
    return entry.get("server_side") == "unsupported"


def split_entries(entries: Iterable[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """(для сервера, только клиент) — включённые установленные моды без повторов."""
    seen, server, client = set(), [], []
    for entry in entries:
        path = entry.get("filepath")
        if not path or entry.get("disabled") or os.path.normcase(path) in seen:
            continue
        seen.add(os.path.normcase(path))
        (client if is_client_only(entry) else server).append(entry)
    return server, client


@dataclass
class Result:
    path: str
    mods: int
    launcher: str                      # Чем запускать: «fabric-server-launch.jar», «install.bat» или «README»
    warnings: List[str] = field(default_factory=list)


def _download(url: str, target: str, session=None):
    from mod_updater_logic import SESSION
    response = (session or SESSION).get(url, timeout=120, stream=True)
    response.raise_for_status()
    with open(target + ".part", "wb") as f:
        for chunk in response.iter_content(256 * 1024):
            f.write(chunk)
    os.replace(target + ".part", target)


def _write(path: str, text: str, executable: bool = False):
    with open(path, "w", encoding="utf-8", newline="\r\n" if path.endswith(".bat") else "\n") as f:
        f.write(text)
    if executable:
        os.chmod(path, 0o755)


def _fabric_server(folder: str, game_version: str, loader_version: str, memory_gb: int, session=None) -> str:
    from mod_updater_logic import SESSION
    installers = (session or SESSION).get(f"{FABRIC_META}/versions/installer", timeout=20).json()
    installer = next((i["version"] for i in installers if i.get("stable")), installers[0]["version"])
    _download(f"{FABRIC_META}/versions/loader/{game_version}/{loader_version}/{installer}/server/jar",
              os.path.join(folder, "fabric-server-launch.jar"), session)
    command = f"java -Xmx{memory_gb}G -jar fabric-server-launch.jar nogui"
    _write(os.path.join(folder, "start.bat"), f"@echo off\n{command}\npause\n")
    _write(os.path.join(folder, "start.sh"), f"#!/bin/sh\n{command}\n", executable=True)
    return "start.bat"


def _installer_server(folder: str, loader: str, game_version: str, loader_version: str, memory_gb: int,
                      session=None) -> str:
    version = loader_version
    if loader == "forge" and version.startswith(game_version + "-"):
        version = version[len(game_version) + 1:]
    name = f"{loader}-installer.jar"
    _download(INSTALLERS[loader].format(g=game_version, v=version), os.path.join(folder, name), session)
    command = f"java -jar {name} --installServer"
    _write(os.path.join(folder, "install.bat"), f"@echo off\n{command}\ndel {name}\necho.\necho Done: start run.bat\npause\n")
    _write(os.path.join(folder, "install.sh"), f"#!/bin/sh\n{command} && rm {name}\n", executable=True)
    _write(os.path.join(folder, "user_jvm_args.txt"), f"-Xmx{memory_gb}G\n")
    return "install.bat"


def _readme(loader: str, game_version: str, loader_version: str, launcher: str, client_only: List[str]) -> str:
    lines = [tr("Сборка для сервера: Minecraft {0}, {1} {2}.", game_version, loader, loader_version), ""]
    if launcher == "start.bat":
        lines += [tr("1. Запустите start.bat (Windows) или start.sh (Linux) — сервер скачает Minecraft и остановится."),
                  tr("2. Прочитайте EULA Minecraft и, если согласны, поставьте eula=true в файле eula.txt."),
                  tr("3. Запустите start.bat ещё раз.")]
    elif launcher == "install.bat":
        lines += [tr("1. Запустите install.bat (Windows) или install.sh (Linux) — установится сервер {0}.", loader),
                  tr("2. Запустите run.bat (run.sh) — сервер создаст eula.txt и остановится."),
                  tr("3. Прочитайте EULA Minecraft и, если согласны, поставьте eula=true в файле eula.txt."),
                  tr("4. Запустите run.bat ещё раз. Память — в user_jvm_args.txt.")]
    else:
        lines += [tr("Установите сервер {0} {1} для Minecraft {2} установщиком с сайта загрузчика и положите рядом "
                     "папки mods и config из этого архива.", loader, loader_version, game_version)]
    lines += ["", tr("Нужна Java той же версии, что и для игры."), "https://www.minecraft.net/eula"]
    if client_only:
        lines += ["", tr("Не вошли (только для клиента):")] + [f"  - {name}" for name in client_only]
    return "\n".join(lines) + "\n"


def build(target_zip: str, mod_paths: List[str], game_dir: str, game_version: str, loader: str, loader_version: str,
          memory_mb: int = 4096, client_only: Optional[List[str]] = None, session=None,
          progress: Callable[[str], None] = lambda text: None) -> Result:
    """Собирает zip для сервера. Без интернета или для неизвестного загрузчика — только моды, настройки и README."""
    memory_gb = max(2, round(memory_mb / 1024))
    warnings: List[str] = []
    with tempfile.TemporaryDirectory(prefix="modupdater-server-") as folder:
        mods = os.path.join(folder, "mods")
        os.makedirs(mods)
        progress(tr("Копируем моды…"))
        for path in mod_paths:
            shutil.copy2(path, os.path.join(mods, enabled_name(os.path.basename(path))))
        for name in CONFIG_FOLDERS:
            source = os.path.join(game_dir, name)
            if os.path.isdir(source):
                shutil.copytree(source, os.path.join(folder, name))
        launcher = "README"
        progress(tr("Скачиваем сервер {0}…", loader))
        try:
            if loader == "fabric":
                launcher = _fabric_server(folder, game_version, loader_version, memory_gb, session)
            elif loader in INSTALLERS:
                launcher = _installer_server(folder, loader, game_version, loader_version, memory_gb, session)
        except Exception as e:
            logging.warning(tr("Сервер {0} не скачан: {1}", loader, e))
            warnings.append(tr("Сервер {0} не скачан ({1}) — в архиве только моды, настройки и инструкция.", loader, e))
        _write(os.path.join(folder, "README_SERVER.txt"),
               _readme(loader, game_version, loader_version, launcher, client_only or []))
        progress(tr("Упаковываем архив…"))
        tmp = target_zip + ".part"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            for root, _, files in os.walk(folder):
                for name in files:
                    full = os.path.join(root, name)
                    info = zipfile.ZipInfo.from_file(full, os.path.relpath(full, folder).replace(os.sep, "/"))
                    if name.endswith(".sh"):
                        info.external_attr = 0o100755 << 16  # Исполняемый после распаковки на Linux
                    with open(full, "rb") as f:
                        archive.writestr(info, f.read(), zipfile.ZIP_DEFLATED)
        os.replace(tmp, target_zip)
    logging.info(tr("Сборка для сервера: {0} модов → {1}", len(mod_paths), target_zip))
    return Result(target_zip, len(mod_paths), launcher, warnings)

