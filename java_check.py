"""
Java и память сборки (без Qt) — две самые частые причины, почему игра не запускается:
  * required_java — какая Java нужна версии игры (1.20.5+ — 21, 26.x — 25, до 1.17 — 8 и т.д.);
  * read_settings — что выставлено в лаунчере: Prism Launcher / MultiMC (instance.cfg и общий .cfg),
    официальный Minecraft Launcher (javaArgs и javaDir установки);
  * recommended_memory / check — сколько памяти нужно по числу модов и что не так перед запуском;
  * set_memory / reset_java — исправление прямо в настройках лаунчера (с копией файла для отмены).
"""
import ctypes
import datetime
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from i18n import tr

INSTANCE_CFG = "instance.cfg"
# Память по умолчанию, если в настройках её нет: Prism — 4 ГБ, MultiMC — 1 ГБ, официальный лаунчер — 2 ГБ
DEFAULT_MEMORY_MB = {"prism": 4096, "multimc": 1024, "official": 2048}
# Аргументы официального лаунчера по умолчанию (с них начинается javaArgs, если своих не было)
OFFICIAL_JAVA_ARGS = ("-Xmx{0}G -XX:+UnlockExperimentalVMOptions -XX:+UseG1GC -XX:G1NewSizePercent=20 "
                      "-XX:G1ReservePercent=20 -XX:MaxGCPauseMillis=50 -XX:G1HeapRegionSize=32M")
MAX_RECOMMENDED_GB = 10
SYSTEM_RESERVE_GB = 3  # Столько оставляем Windows и остальным программам
FIXABLE = ("prism", "multimc", "official")


# --- Какая Java нужна ---

def _game_parts(game_version: str) -> Optional[Tuple[int, int, int]]:
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", (game_version or "").strip())
    return tuple(int(x or 0) for x in match.groups()) if match else None


def required_java(game_version: str, loader: Optional[str] = None) -> Optional[Tuple[int, Optional[int]]]:
    """(наименьшая, наибольшая или None) версия Java для версии игры. None — версия неизвестна (снапшот)."""
    parts = _game_parts(game_version)
    if not parts:
        return None
    major, minor, patch = parts
    if major >= 26:  # Новая нумерация Minecraft (26.1 и дальше) — Java 25
        return 25, None
    if major != 1:
        return None
    if minor >= 21 or (minor == 20 and patch >= 5):
        return 21, None
    if minor >= 18:
        return 17, None
    if minor == 17:
        return 16, None
    # Старый Forge (LaunchWrapper) на Java 9+ не запускается
    return 8, (8 if loader == "forge" and minor <= 12 else None)


def java_major(version: str) -> Optional[int]:
    """«21.0.7» -> 21, «1.8.0_392» -> 8."""
    match = re.match(r"^(\d+)(?:\.(\d+))?", (version or "").strip())
    if not match:
        return None
    first = int(match.group(1))
    return int(match.group(2) or 0) if first == 1 else first


_VERSION_CACHE: Dict[Tuple[str, float], Optional[int]] = {}


def java_version_of(path: str) -> Optional[int]:
    """Версия Java по исполняемому файлу (java -version). javaw.exe ничего не пишет — берём java.exe рядом."""
    if not path:
        return None
    if os.path.isdir(path):
        path = os.path.join(path, "bin", "java.exe" if sys.platform == "win32" else "java")
    folder, name = os.path.split(path)
    if name.lower() == "javaw.exe":
        path = os.path.join(folder, "java.exe")
    try:
        key = (os.path.normcase(path), os.path.getmtime(path))
    except OSError:
        return None
    if key not in _VERSION_CACHE:
        try:
            result = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=15,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            match = re.search(r'version "([^"]+)"', result.stderr + result.stdout)
            _VERSION_CACHE[key] = java_major(match.group(1)) if match else None
        except (OSError, subprocess.SubprocessError):
            _VERSION_CACHE[key] = None
    return _VERSION_CACHE[key]


# --- Память ---

def total_memory_mb() -> Optional[int]:
    """Объём оперативной памяти компьютера (Windows). None — неизвестно."""
    if sys.platform != "win32":
        return None

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(MemoryStatus)
    try:
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullTotalPhys // (1024 * 1024))
    except (AttributeError, OSError):
        pass
    return None


def recommended_memory(mod_count: int, total_mb: Optional[int] = None) -> int:
    """Сколько памяти выделить игре, МБ: 2 ГБ + 1 ГБ на каждые 50 модов, но не больше, чем позволяет компьютер."""
    gb = min(MAX_RECOMMENDED_GB, 2 + math.ceil(max(0, mod_count) / 50))
    if total_mb:
        gb = max(2, min(gb, total_mb // 1024 - SYSTEM_RESERVE_GB))
    return gb * 1024


def parse_xmx(args: str) -> Optional[int]:
    """-Xmx из аргументов Java, МБ."""
    match = re.search(r"-Xmx(\d+)([kKmMgG]?)", args or "")
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2).lower()
    return {"g": value * 1024, "m": value, "k": value // 1024}.get(unit, value // (1024 * 1024))


def format_gb(mb: int) -> str:
    gb = mb / 1024
    return tr("{0} ГБ", f"{gb:.0f}" if gb == int(gb) else f"{gb:.1f}".replace(".", ","))


# --- Настройки лаунчера ---

@dataclass
class Settings:
    launcher: str                      # prism / multimc / official
    memory_mb: Optional[int] = None    # Выделено игре (-Xmx)
    java: Optional[int] = None         # Версия Java, на которой лаунчер запустит игру (если известна)
    java_auto: bool = False            # Лаунчер сам подбирает Java под версию игры
    paths: Dict[str, str] = field(default_factory=dict)


def _read_cfg(path: str) -> Dict[str, str]:
    values = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "=" in line and not line.startswith(("[", ";", "#")):
                    key, value = line.rstrip("\r\n").split("=", 1)
                    values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def _true(value: Optional[str]) -> bool:
    return (value or "").strip().lower() == "true"


def _prism_paths(game_dir: str, launcher: str) -> Tuple[str, str]:
    """(instance.cfg, общий prismlauncher.cfg / multimc.cfg)."""
    from game_runner import instance_dir
    inst = instance_dir(game_dir)
    name = "prismlauncher.cfg" if launcher == "prism" else "multimc.cfg"
    candidates = [os.path.join(os.path.dirname(os.path.dirname(inst)), name)]
    if launcher == "prism":
        candidates.append(os.path.join(os.getenv("APPDATA") or "", "PrismLauncher", name))
    return os.path.join(inst, INSTANCE_CFG), next((c for c in candidates if os.path.isfile(c)), candidates[0])


def _official_installation(profile: Dict) -> Tuple[str, Optional[Dict]]:
    import official_launcher
    mc_dir = official_launcher.minecraft_dir()
    try:
        data = official_launcher.read_profiles(mc_dir)
    except (OSError, ValueError):
        return mc_dir, None
    item = (data.get("profiles") or {}).get(profile.get("launcher_id") or "")
    return mc_dir, item if isinstance(item, dict) else None


def read_settings(profile: Dict) -> Optional[Settings]:
    """Память и Java сборки по настройкам её лаунчера. None — лаунчер не поддерживается или настройки не найдены."""
    launcher, game_dir = profile.get("source") or "", profile.get("mods_path") or ""
    if launcher in ("prism", "multimc") and game_dir:
        inst_path, global_path = _prism_paths(game_dir, launcher)
        if not os.path.isfile(inst_path):
            return None
        inst, glob = _read_cfg(inst_path), _read_cfg(global_path)
        settings = Settings(launcher, paths={"instance": inst_path, "global": global_path})
        memory = inst.get("MaxMemAlloc") if _true(inst.get("OverrideMemory")) else glob.get("MaxMemAlloc")
        settings.memory_mb = int(memory) if (memory or "").isdigit() else DEFAULT_MEMORY_MB[launcher]
        global_auto = launcher == "prism" and _true(glob.get("AutomaticJavaSwitch"))
        if _true(inst.get("OverrideJavaLocation")):
            # Своя Java сборки: выбрана вручную или запомнена Prism при прошлом автовыборе — после смены
            # версии игры она может перестать подходить, поэтому проверяем её в обоих случаях
            settings.java = java_major(inst.get("JavaVersion", "")) or java_version_of(inst.get("JavaPath", ""))
        else:
            settings.java_auto = global_auto  # Prism подберёт Java при запуске
            settings.java = java_major(glob.get("JavaVersion", "")) or java_version_of(glob.get("JavaPath", ""))
        return settings
    if launcher == "official" and profile.get("launcher_id"):
        mc_dir, item = _official_installation(profile)
        if item is None:
            return None
        settings = Settings(launcher, paths={"minecraft": mc_dir})
        settings.memory_mb = parse_xmx(item.get("javaArgs") or "") or DEFAULT_MEMORY_MB[launcher]
        if item.get("javaDir"):
            settings.java = java_version_of(item["javaDir"])
        else:
            settings.java_auto = True  # Своя Java лаунчера под каждую версию игры
        return settings
    return None


# --- Проверка перед запуском ---

@dataclass
class Issue:
    kind: str     # "java" | "memory" | "memory_high"
    text: str
    fix: str = ""     # Подпись кнопки исправления; пусто — исправить можно только вручную
    value: int = 0    # Для памяти — сколько выставить, МБ


def check(profile: Dict, mod_count: int, settings: Optional[Settings] = None,
          total_mb: Optional[int] = None) -> List[Issue]:
    """Что помешает запуску: не та Java, мало памяти для стольких модов, памяти больше, чем есть в компьютере."""
    settings = settings or read_settings(profile)
    if settings is None:
        return []
    issues = []
    need = required_java(profile.get("game_version") or "", profile.get("loader"))
    if need and settings.java and not settings.java_auto:
        low, high = need
        if settings.java < low or (high and settings.java > high):
            wanted = tr("Java {0}", low) if high == low else tr("Java {0} или новее", low)
            text = tr("Minecraft {0} нужна {1}, а лаунчер запустит игру на Java {2}.",
                      profile.get("game_version"), wanted, settings.java)
            fix = tr("Выбирать Java автоматически") if settings.launcher in ("prism", "official") else ""
            issues.append(Issue("java", text, fix))
    total_mb = total_mb if total_mb is not None else total_memory_mb()
    if settings.memory_mb:
        want = recommended_memory(mod_count, total_mb)
        if total_mb and settings.memory_mb > total_mb - 2048:
            issues.append(Issue("memory_high", tr(
                "Игре выделено {0}, а в компьютере всего {1} — Windows начнёт тормозить или игра не запустится.",
                format_gb(settings.memory_mb), format_gb(total_mb)), tr("Выставить {0}", format_gb(want)), want))
        elif settings.memory_mb < want - 1024:
            issues.append(Issue("memory", tr("Игре выделено {0} — для {1} модов этого мало, лучше {2}.",
                                             format_gb(settings.memory_mb), mod_count, format_gb(want)),
                                tr("Выставить {0}", format_gb(want)), want))
    return issues


# --- Исправление в настройках лаунчера ---

def _backup(path: str) -> str:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = f"{path}.modupdater-{stamp}.bak"
    shutil.copy2(path, backup)
    return backup


def _set_cfg(path: str, values: Dict[str, Optional[str]]) -> str:
    """Меняет ключи instance.cfg (None — удалить), остальное не трогает. Возвращает путь копии файла."""
    backup = _backup(path)
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.read().splitlines()
    left, result = dict(values), []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.startswith("[") else None
        if key in left:
            value = left.pop(key)
            if value is not None:
                result.append(f"{key}={value}")
            continue
        result.append(line)
    additions = [f"{k}={v}" for k, v in left.items() if v is not None]
    if additions:  # Новые ключи — в конец секции [General]
        try:
            start = result.index("[General]") + 1
            end = next((i for i in range(start, len(result)) if result[i].startswith("[")), len(result))
        except ValueError:
            end = len(result)
        result[end:end] = additions
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(result) + "\n")
    os.replace(tmp, path)
    return backup


def _update_official(profile: Dict, change) -> str:
    import official_launcher
    mc_dir = official_launcher.minecraft_dir()
    data = official_launcher.read_profiles(mc_dir)
    item = (data.get("profiles") or {}).get(profile.get("launcher_id") or "")
    if not isinstance(item, dict):
        raise ValueError(tr("Установка не найдена в Minecraft Launcher — возможно, её удалили."))
    change(item)
    return official_launcher._write(mc_dir, data)


def set_memory(profile: Dict, memory_mb: int) -> Tuple[str, str]:
    """Выделяет игре memory_mb в настройках лаунчера. Возвращает (файл, копия) для отмены."""
    settings = read_settings(profile)
    if settings is None or settings.launcher not in FIXABLE:
        raise ValueError(tr("Память этой сборки меняется только в её лаунчере."))
    if settings.launcher == "official":
        gb = max(1, round(memory_mb / 1024))

        def change(item):
            args = item.get("javaArgs")
            if args and parse_xmx(args):
                item["javaArgs"] = re.sub(r"-Xmx\d+[kKmMgG]?", f"-Xmx{gb}G", args)
            else:
                item["javaArgs"] = (f"-Xmx{gb}G " + args) if args else OFFICIAL_JAVA_ARGS.format(gb)
        import official_launcher
        return official_launcher.profiles_path(settings.paths["minecraft"]), _update_official(profile, change)
    path = settings.paths["instance"]
    inst = _read_cfg(path)
    values = {"OverrideMemory": "true", "MaxMemAlloc": str(memory_mb)}
    minimum = inst.get("MinMemAlloc") if _true(inst.get("OverrideMemory")) else None
    values["MinMemAlloc"] = str(min(int(minimum), memory_mb) if (minimum or "").isdigit() else min(512, memory_mb))
    return path, _set_cfg(path, values)


def reset_java(profile: Dict) -> Tuple[str, str]:
    """Отдаёт выбор Java лаунчеру: Prism подберёт подходящую (или скачает), официальный возьмёт свою."""
    settings = read_settings(profile)
    if settings is None or settings.launcher not in ("prism", "official"):
        raise ValueError(tr("Java этой сборки меняется только в её лаунчере."))
    if settings.launcher == "official":
        import official_launcher
        return (official_launcher.profiles_path(settings.paths["minecraft"]),
                _update_official(profile, lambda item: item.pop("javaDir", None)))
    if not _true(_read_cfg(settings.paths["global"]).get("AutomaticJavaSwitch")):
        raise ValueError(tr("В Prism выключен автоматический выбор Java: Настройки → Java → «Автоматически "
                            "переключаться на подходящую Java»."))
    path = settings.paths["instance"]
    return path, _set_cfg(path, {"OverrideJavaLocation": "false", "AutomaticJava": None, "JavaPath": None,
                                 "JavaVersion": None})


def restore(path: str, backup: str):
    shutil.copy2(backup, path)


def launcher_running(profile: Dict) -> bool:
    """Открыт ли лаунчер сборки: пока он открыт, правка его настроек не подхватится (и он её затрёт)."""
    if profile.get("source") == "official":
        import official_launcher
        return official_launcher.launcher_running()
    import launcher_pack
    return launcher_pack.launcher_running()
