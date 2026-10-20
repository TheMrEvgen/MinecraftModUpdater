"""
Запуск игры и наблюдение за ней (без Qt):
  * команда запуска сборки: Prism Launcher / MultiMC открывают нужную сборку сразу, остальные лаунчеры —
    просто открываются (выбрать сборку там нужно самому), для своих случаев — команда из настроек сборки;
  * состояние запуска по логу игры: загружается, дошла до главного меню, вылетела;
  * поиск и закрытие процессов игры этой сборки (для автоматического поиска проблемного мода).
"""
import glob
import json
import logging
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import List, Optional
from i18n import tr

CREATE_NO_WINDOW = 0x08000000
GAME_DIR_NAMES = ("minecraft", ".minecraft")

# Строки лога: игра дошла до главного меню
MENU_MARKERS = ("Sound engine started", "Narrator library successfully loaded", "OpenAL initialized")
# Строки лога: запуск не удался (загрузчик или игра упали)
CRASH_MARKERS = ("---- Minecraft Crash Report ----", "Minecraft has crashed", "Incompatible mods found",
                 "Incompatible mod set", "Mod resolution failed", "FormattedException",
                 "Exception in thread \"main\"", "Mixin apply failed", "Mixin transformation of",
                 "Encountered an unexpected exception", "Failed to start the minecraft server",
                 "Game crashed! Crash report saved to", "Mod loading has failed", "Missing or unsupported mandatory dependencies")

# Лаунчеры по полю source профиля
LAUNCHER_TITLES = {"prism": "Prism Launcher", "multimc": "MultiMC", "modrinth": "Modrinth App",
                   "curseforge": "CurseForge", "official": "Minecraft Launcher"}


@dataclass
class LaunchPlan:
    command: List[str]
    title: str           # Что запускаем — для подписи кнопки и уведомлений
    opens_instance: bool  # True — сборка запускается сразу; False — откроется только лаунчер


def _env(name: str, default: str = "") -> str:
    return os.getenv(name) or default


def _first_file(*paths: str) -> Optional[str]:
    return next((p for p in paths if p and os.path.isfile(p)), None)


def instance_dir(game_dir: str) -> str:
    """Папка сборки Prism/MultiMC: игра лежит в её подпапке minecraft или .minecraft."""
    game_dir = os.path.normpath(game_dir)
    return os.path.dirname(game_dir) if os.path.basename(game_dir).lower() in GAME_DIR_NAMES else game_dir


def find_prism_exe(data_root: str) -> Optional[str]:
    local, programs = _env("LOCALAPPDATA"), _env("ProgramFiles", r"C:\Program Files")
    return _first_file(os.path.join(data_root, "prismlauncher.exe"),  # Портативная версия
                       os.path.join(local, "Programs", "PrismLauncher", "prismlauncher.exe"),
                       os.path.join(programs, "PrismLauncher", "prismlauncher.exe"),
                       os.path.join(os.path.expanduser("~"), "scoop", "apps", "prismlauncher", "current", "prismlauncher.exe"))


def find_multimc_exe(data_root: str) -> Optional[str]:
    return _first_file(os.path.join(data_root, "MultiMC.exe"))


def find_modrinth_exe() -> Optional[str]:
    local, programs = _env("LOCALAPPDATA"), _env("ProgramFiles", r"C:\Program Files")
    return _first_file(os.path.join(local, "Programs", "Modrinth App", "Modrinth App.exe"),
                       os.path.join(local, "Modrinth App", "Modrinth App.exe"),
                       os.path.join(programs, "Modrinth App", "Modrinth App.exe"))


def find_curseforge_exe() -> Optional[str]:
    local = _env("LOCALAPPDATA")
    return _first_file(os.path.join(local, "Programs", "CurseForge Windows", "CurseForge.exe"),
                       os.path.join(local, "Programs", "CurseForge", "CurseForge.exe"))


def find_official_launcher() -> Optional[str]:
    x86 = _env("ProgramFiles(x86)", r"C:\Program Files (x86)")
    return _first_file(os.path.join(x86, "Minecraft Launcher", "MinecraftLauncher.exe"),
                       r"C:\XboxGames\Minecraft Launcher\Content\Minecraft.exe")


def launch_plan(profile: dict) -> Optional[LaunchPlan]:
    """Как запустить сборку. None — способ неизвестен (можно задать свою команду в настройках сборки)."""
    custom = (profile.get("launch_command") or "").strip()
    if custom:
        parts = [part[1:-1] if len(part) > 1 and part[0] == part[-1] == '"' else part
                 for part in shlex.split(custom, posix=False)]  # Пути в кавычках — без кавычек
        return LaunchPlan(parts, tr("своя команда из настроек сборки"), True)
    game_dir = profile.get("mods_path") or ""
    source = profile.get("source") or "manual"
    if source in ("prism", "multimc") and game_dir:
        inst = instance_dir(game_dir)
        data_root = os.path.dirname(os.path.dirname(inst))  # …/PrismLauncher/instances/<сборка>
        exe = find_prism_exe(data_root) if source == "prism" else find_multimc_exe(data_root)
        if exe:
            return LaunchPlan([exe, "--launch" if source == "prism" else "-l", os.path.basename(inst)],
                              LAUNCHER_TITLES[source], True)
    if source == "modrinth":
        exe = find_modrinth_exe()
        if exe:
            return LaunchPlan([exe], LAUNCHER_TITLES[source], False)
    if source == "curseforge":
        exe = find_curseforge_exe()
        if exe:
            return LaunchPlan([exe], LAUNCHER_TITLES[source], False)
    if source == "official" or (source == "manual" and sys.platform == "win32"):
        exe = find_official_launcher()
        if exe:
            return LaunchPlan([exe], LAUNCHER_TITLES["official"], False)
        if sys.platform == "win32" and source == "official":  # Лаунчер из Microsoft Store
            return LaunchPlan(["explorer.exe", r"shell:AppsFolder\Microsoft.4297127D64EC6_8wekyb3d8bbwe!Minecraft"],
                              LAUNCHER_TITLES["official"], False)
    return None


def launch(plan: LaunchPlan) -> None:
    flags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
    subprocess.Popen(plan.command, creationflags=flags, close_fds=True,
                     cwd=os.path.dirname(plan.command[0]) if os.path.isabs(plan.command[0]) else None)
    logging.info(f"Запуск игры: {' '.join(plan.command)}")


# --- Наблюдение по логу ---

@dataclass
class RunState:
    log_seen: bool = False    # Игра начала писать новый лог
    reached_menu: bool = False
    crashed: bool = False
    crash_file: Optional[str] = None
    reason: str = ""


def _read_since(path: str, since: float) -> Optional[str]:
    try:
        if os.path.getmtime(path) < since:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()[-3_000_000:]
    except OSError:
        return None


def newest_crash_report(game_dir: str, since: float) -> Optional[str]:
    reports = [p for p in glob.glob(os.path.join(game_dir, "crash-reports", "*.txt")) if os.path.getmtime(p) >= since]
    return max(reports, key=os.path.getmtime) if reports else None


def run_state(game_dir: str, since: float) -> RunState:
    """Что известно о запуске, начатом в момент since (по latest.log и папке crash-reports)."""
    state = RunState()
    report = newest_crash_report(game_dir, since)
    if report:
        state.crashed, state.crash_file, state.reason = True, report, tr("создан краш-репорт")
    log_path = os.path.join(game_dir, "logs", "latest.log")
    text = _read_since(log_path, since - 1)
    if text is not None:
        state.log_seen = True
        state.reached_menu = any(marker in text for marker in MENU_MARKERS)
        crash = next((marker for marker in CRASH_MARKERS if marker in text), None)
        if crash and not state.crashed:
            state.crashed, state.crash_file, state.reason = True, log_path, tr('в логе: «{0}»', crash)
    return state


def ended_normally(game_dir: str, since: float) -> bool:
    text = _read_since(os.path.join(game_dir, "logs", "latest.log"), since - 1) or ""
    return "Stopping!" in text and not any(marker in text for marker in CRASH_MARKERS)


# --- Процессы игры ---

def find_game_processes(game_dir: str) -> List[int]:
    """PID процессов Java, запущенных для этой папки игры (по командной строке)."""
    if sys.platform != "win32" or not game_dir:
        return []
    script = ("Get-CimInstance Win32_Process -Filter \"Name like 'java%'\" | "
              "Where-Object { $_.CommandLine -like ('*' + $env:MU_GAME_DIR + '*') } | "
              "Select-Object -ExpandProperty ProcessId | ConvertTo-Json -Compress")
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                env={**os.environ, "MU_GAME_DIR": os.path.normpath(game_dir)}, capture_output=True,
                                text=True, timeout=20, creationflags=CREATE_NO_WINDOW)
        data = json.loads(result.stdout or "null")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if data is None:
        return []
    return [int(pid) for pid in (data if isinstance(data, list) else [data])]


def stop_game(game_dir: str) -> int:
    """Закрывает игру этой сборки. Возвращает число завершённых процессов."""
    pids = find_game_processes(game_dir)
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=CREATE_NO_WINDOW)
    if pids:
        logging.info(f"Игра закрыта (процессы {pids}).")
        time.sleep(1.5)  # Даём Windows освободить файлы модов
    return len(pids)
