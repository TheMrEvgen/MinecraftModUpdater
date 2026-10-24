"""
Автопоиск установок Minecraft в популярных лаунчерах.
Каждая найденная установка описывается как профиль: папка игры, версия, загрузчик и его версия.
"""
import configparser
import json
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional

from game_detection import detect_game_versions, extract_game_version, extract_loader, extract_loader_version
from i18n import tr

# uid компонентов Prism/MultiMC -> загрузчик
_PRISM_LOADER_UIDS = {
    "net.fabricmc.fabric-loader": "fabric",
    "org.quiltmc.quilt-loader": "quilt",
    "net.neoforged": "neoforge",
    "net.minecraftforge": "forge",
}

SOURCE_LABELS = {
    "manual": tr("Вручную"),
    "official": "Minecraft Launcher",
    "prism": "Prism Launcher",
    "multimc": "MultiMC",
    "modrinth": "Modrinth App",
    "curseforge": "CurseForge",
    "legacy": "Legacy Launcher",
}


@dataclass
class Instance:
    name: str
    game_dir: str
    source: str
    game_version: Optional[str] = None
    loader: Optional[str] = None
    loader_version: Optional[str] = None

    def to_profile(self, channel: str) -> Dict:
        has_meta = bool(self.game_version and self.loader)
        return {
            "mods_path": self.game_dir,
            "game_version": self.game_version or "",
            "loader": self.loader or "fabric",
            # Версии из метаданных лаунчера точнее автоопределения по папке versions
            "manual_mode": has_meta and self.source != "official",
            "channel": channel,
            "source": self.source,
            "loader_version": self.loader_version or "",
        }


def _appdata() -> str:
    if sys.platform == "win32":
        return os.getenv("APPDATA") or os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support")
    return os.getenv("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")


def _read_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _first_existing_dir(*paths: str) -> Optional[str]:
    return next((p for p in paths if p and os.path.isdir(p)), None)


# --- Prism Launcher / MultiMC ---

def _prism_instance_dir(root: str) -> str:
    """Папка экземпляров может быть переопределена в prismlauncher.cfg / multimc.cfg."""
    for cfg_name in ("prismlauncher.cfg", "multimc.cfg"):
        cfg_path = os.path.join(root, cfg_name)
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if line.startswith("InstanceDir="):
                            value = line.split("=", 1)[1].strip()
                            return value if os.path.isabs(value) else os.path.join(root, value)
            except OSError:
                pass
    return os.path.join(root, "instances")


def _read_instance_cfg_name(path: str) -> Optional[str]:
    try:
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            parser.read_string("[root]\n" + f.read())
        return parser.get("root", "name", fallback=None)
    except (OSError, configparser.Error):
        return None


def find_prism_instances(root: str, source: str = "prism") -> List[Instance]:
    instances_dir = _prism_instance_dir(root)
    if not os.path.isdir(instances_dir):
        return []
    result = []
    for entry in os.scandir(instances_dir):
        if not entry.is_dir() or entry.name.startswith(('.', '_')):
            continue
        game_dir = _first_existing_dir(os.path.join(entry.path, "minecraft"), os.path.join(entry.path, ".minecraft"))
        pack_path = os.path.join(entry.path, "mmc-pack.json")
        if not game_dir or not os.path.isfile(pack_path):
            continue
        instance = Instance(name=_read_instance_cfg_name(os.path.join(entry.path, "instance.cfg")) or entry.name,
                            game_dir=game_dir, source=source)
        try:
            for component in _read_json(pack_path).get("components", []):
                uid = component.get("uid")
                if uid == "net.minecraft":
                    instance.game_version = component.get("version")
                elif uid in _PRISM_LOADER_UIDS:
                    instance.loader = _PRISM_LOADER_UIDS[uid]
                    instance.loader_version = component.get("version")
        except (OSError, json.JSONDecodeError, AttributeError) as e:
            logging.debug(f"Не удалось прочитать {pack_path}: {e}")
        result.append(instance)
    return result


# --- Modrinth App ---

def find_modrinth_instances(root: str) -> List[Instance]:
    profiles_dir = os.path.join(root, "profiles")
    if not os.path.isdir(profiles_dir):
        return []
    meta: Dict[str, tuple] = {}
    db_path = os.path.join(root, "app.db")
    if os.path.isfile(db_path):
        try:
            # Только чтение: открываем базу приложения в режиме ro, чтобы ничего не заблокировать
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
            try:
                for row in conn.execute("SELECT path, name, game_version, mod_loader, mod_loader_version FROM profiles"):
                    meta[row[0]] = row[1:]
            finally:
                conn.close()
        except sqlite3.Error as e:
            logging.debug(f"Не удалось прочитать базу Modrinth App: {e}")

    result = []
    for entry in os.scandir(profiles_dir):
        if not entry.is_dir():
            continue
        name, game_version, loader, loader_version = meta.get(entry.name, (entry.name, None, None, None))
        loader = (loader or "").lower() or None
        if loader == "vanilla":
            loader = None
        result.append(Instance(name=name or entry.name, game_dir=entry.path, source="modrinth",
                               game_version=game_version, loader=loader, loader_version=loader_version))
    return result


# --- CurseForge ---

def find_curseforge_instances(root: str) -> List[Instance]:
    if not os.path.isdir(root):
        return []
    result = []
    for entry in os.scandir(root):
        meta_path = os.path.join(entry.path, "minecraftinstance.json")
        if not entry.is_dir() or not os.path.isfile(meta_path):
            continue
        instance = Instance(name=entry.name, game_dir=entry.path, source="curseforge")
        try:
            data = _read_json(meta_path)
            instance.name = data.get("name") or entry.name
            base = data.get("baseModLoader") or {}
            instance.game_version = base.get("minecraftVersion") or data.get("gameVersion")
            loader_name = str(base.get("name", ""))  # например 'forge-47.2.0' или 'fabric-0.15.0-1.20.1'
            for loader in ("neoforge", "fabric", "quilt", "forge"):
                if loader_name.lower().startswith(loader):
                    instance.loader = loader
                    instance.loader_version = loader_name.split("-")[1] if "-" in loader_name else None
                    break
        except (OSError, json.JSONDecodeError, AttributeError, IndexError) as e:
            logging.debug(f"Не удалось прочитать {meta_path}: {e}")
        result.append(instance)
    return result


# --- Официальный лаунчер ---

def find_official_instance(game_dir: str) -> List[Instance]:
    if not os.path.isdir(os.path.join(game_dir, "mods")):
        return []
    detected = detect_game_versions(game_dir)
    instance = Instance(name="Minecraft (.minecraft)", game_dir=game_dir, source="official")
    if detected.best_guess:
        instance.game_version, instance.loader = detected.best_guess
        instance.loader_version = extract_loader_version(instance.loader, detected.best_guess_id or "")
    return [instance]


# --- Legacy Launcher (legacylauncher.ru; не путать с TLauncher) ---

def find_legacy_installs() -> List[str]:
    """Папки установки Legacy Launcher из «Установки и удаления программ» (издатель «Legacy Launcher Team»)."""
    if sys.platform != "win32":
        return []
    import winreg
    result = []
    roots = ((winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"))
    for hive, path in roots:
        try:
            with winreg.OpenKey(hive, path) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    values = {}
                    try:
                        with winreg.OpenKey(key, winreg.EnumKey(key, i)) as app:
                            for name in ("DisplayName", "Publisher", "InstallLocation"):
                                try:
                                    values[name] = str(winreg.QueryValueEx(app, name)[0])
                                except OSError:
                                    values[name] = ""
                    except OSError:
                        continue
                    is_legacy = "legacy launcher" in values["DisplayName"].lower() \
                        or values["Publisher"] == "Legacy Launcher Team"
                    if is_legacy and values["InstallLocation"] and os.path.isdir(values["InstallLocation"]):
                        result.append(os.path.normpath(values["InstallLocation"]))
        except OSError:
            continue
    return list(dict.fromkeys(result))


def read_properties(path: str) -> Dict[str, str]:
    r"""Файл .properties (Java): «ключ=значение» с экранированием «\:», «\\» и \uXXXX."""
    result = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return result
    for line in lines:
        line = line.strip()
        if not line or line.startswith(("#", "!")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), value.strip())
        result[key.strip()] = re.sub(r"\\(.)", r"\1", value)
    return result


def _legacy_versions(game_dir: str, version_id: str) -> tuple:
    """(версия игры, загрузчик, версия загрузчика) по выбранной в лаунчере версии или по папке versions."""
    data = _read_json(os.path.join(game_dir, "versions", version_id, f"{version_id}.json")) if version_id else None
    if isinstance(data, dict):
        loader = extract_loader(version_id)
        return extract_game_version(data), loader, extract_loader_version(loader, version_id) if loader else None
    detected = detect_game_versions(game_dir)
    if detected.best_guess:
        game, loader = detected.best_guess
        return game, loader, extract_loader_version(loader, detected.best_guess_id or "")
    return None, None, None


def find_legacy_instances(appdata: Optional[str] = None, installs: Optional[List[str]] = None) -> List[Instance]:
    r"""Папка игры Legacy Launcher: из tl.properties рядом с лаунчером или %APPDATA%\.tlauncher\legacy.properties."""
    appdata = appdata or _appdata()
    configs = [os.path.join(d, "tl.properties") for d in (find_legacy_installs() if installs is None else installs)]
    configs.append(os.path.join(appdata, ".tlauncher", "legacy.properties"))
    configs = sorted((c for c in configs if os.path.isfile(c)), key=os.path.getmtime, reverse=True)
    result, seen = [], set()
    for config in configs:  # Самая свежая настройка — первой
        props = read_properties(config)
        game_dir = props.get("minecraft.gamedir") or os.path.join(appdata, ".minecraft")
        key = os.path.normcase(os.path.normpath(game_dir))
        if key in seen or not os.path.isdir(game_dir):
            continue
        seen.add(key)
        version_id = props.get("login.version", "")
        game, loader, loader_version = _legacy_versions(game_dir, "" if version_id.startswith("latest") else version_id)
        name = "Legacy Launcher" if not result else f"Legacy Launcher ({os.path.basename(game_dir)})"
        result.append(Instance(name=name, game_dir=game_dir, source="legacy", game_version=game, loader=loader,
                               loader_version=loader_version))
    return result


def discover_instances() -> List[Instance]:
    """Находит установки во всех известных лаунчерах. Ошибки одного лаунчера не мешают остальным."""
    appdata = _appdata()
    home = os.path.expanduser("~")
    finders = [
        lambda: find_official_instance(os.path.join(appdata, ".minecraft") if sys.platform == "win32"
                                       else os.path.join(home, ".minecraft")),
        lambda: find_prism_instances(os.path.join(appdata, "PrismLauncher"), "prism"),
        lambda: find_prism_instances(os.path.join(appdata, "MultiMC"), "multimc"),
        lambda: find_modrinth_instances(os.path.join(appdata, "ModrinthApp")),
        lambda: find_modrinth_instances(os.path.join(appdata, "com.modrinth.theseus")),
        lambda: find_curseforge_instances(os.path.join(home, "curseforge", "minecraft", "Instances")),
        lambda: find_legacy_instances(appdata),
    ]
    result: List[Instance] = []
    seen = set()
    for finder in finders:
        try:
            for instance in finder():
                key = os.path.normcase(os.path.normpath(instance.game_dir))
                # Ванильные сборки без загрузчика и без папки mods обновлять нечего
                if not instance.loader and not os.path.isdir(os.path.join(instance.game_dir, "mods")):
                    continue
                if key not in seen:
                    seen.add(key)
                    result.append(instance)
        except OSError as e:
            logging.debug(f"Ошибка поиска установок: {e}")
    logging.info(tr('Найдено установок в лаунчерах: {0}', len(result)))
    return result
