"""
Установка программы из неё самой (Windows, без прав администратора — всё в профиле пользователя):
копия .exe в %LOCALAPPDATA%\\Programs, ярлыки в «Пуске» и на рабочем столе, пункт в «Установке и удалении
программ», открытие .mrpack двойным щелчком и ссылки modupdater://. Удаление — из программы или из «Параметров».
"""
import ctypes
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional

import config as app_config

APP_TITLE = "Minecraft Mod Updater"
EXE_NAME = "ModUpdater.exe"
UNINSTALL_KEY = "MinecraftModUpdater"
MRPACK_PROGID = "MinecraftModUpdater.mrpack"
PROTOCOL = "modupdater"
CREATE_NO_WINDOW = 0x08000000


@dataclass
class InstallPaths:
    """Пути установки. В тестах подменяются на временные (registry_root — свой раздел реестра)."""
    install_dir: str
    start_menu_dir: str
    desktop_dir: str
    registry_root: str = "Software"

    @property
    def exe(self) -> str:
        return os.path.join(self.install_dir, EXE_NAME)

    @property
    def start_shortcut(self) -> str:
        return os.path.join(self.start_menu_dir, f"{APP_TITLE}.lnk")

    @property
    def desktop_shortcut(self) -> str:
        return os.path.join(self.desktop_dir, f"{APP_TITLE}.lnk")


def default_paths() -> InstallPaths:
    local = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
    roaming = os.getenv("APPDATA") or os.path.expanduser("~")
    return InstallPaths(os.path.join(local, "Programs", APP_TITLE),
                        os.path.join(roaming, "Microsoft", "Windows", "Start Menu", "Programs"),
                        _desktop_dir())


def _desktop_dir() -> str:
    if sys.platform == "win32":
        try:
            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0:  # CSIDL_DESKTOPDIRECTORY
                return buf.value
        except (OSError, AttributeError):
            pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def supported() -> bool:
    """Установка возможна только для собранной программы (.exe) на Windows."""
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


# --- Реестр ---

def _winreg():
    import winreg
    return winreg


def _set_values(path: str, values: Dict[str, object]):
    winreg = _winreg()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
        for name, value in values.items():
            kind = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
            winreg.SetValueEx(key, name, 0, kind, value)


def _delete_tree(path: str):
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _delete_tree(f"{path}\\{child}")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        pass


def _read_value(path: str, name: str = "") -> Optional[str]:
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except OSError:
        return None


def _keys(paths: InstallPaths) -> Dict[str, str]:
    root = paths.registry_root
    return {
        "uninstall": f"{root}\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{UNINSTALL_KEY}",
        "mrpack_ext": f"{root}\\Classes\\.mrpack\\OpenWithProgids",
        "mrpack_progid": f"{root}\\Classes\\{MRPACK_PROGID}",
        "mrpack_default": f"{root}\\Classes\\.mrpack",
        "protocol": f"{root}\\Classes\\{PROTOCOL}",
    }


def _notify_shell():
    """Проводник сразу узнаёт о новых ассоциациях файлов."""
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)  # SHCNE_ASSOCCHANGED
    except (OSError, AttributeError):
        pass


# --- Ярлыки ---

def create_shortcut(path: str, target: str, description: str = APP_TITLE):
    """Ярлык .lnk через WScript.Shell (стандартный компонент Windows)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:MU_LNK); $s.TargetPath = $env:MU_TARGET; "
              "$s.WorkingDirectory = Split-Path $env:MU_TARGET; $s.IconLocation = \"$env:MU_TARGET,0\"; "
              "$s.Description = $env:MU_DESC; $s.Save()")
    env = {**os.environ, "MU_LNK": path, "MU_TARGET": target, "MU_DESC": description}
    flags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=env,
                            capture_output=True, creationflags=flags, timeout=30)
    if result.returncode != 0:  # Значок не прочитался — ярлык без своего значка всё равно лучше, чем никакого
        script = script.replace(' $s.IconLocation = \"$env:MU_TARGET,0\";', "")
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=env, check=True,
                       capture_output=True, creationflags=flags, timeout=30)


# --- Состояние ---

@dataclass
class InstallState:
    installed: bool
    installed_version: Optional[str]
    running_installed: bool   # Запущена именно установленная копия
    exe: str
    mrpack: bool
    protocol: bool
    desktop: bool


def state(paths: Optional[InstallPaths] = None) -> InstallState:
    paths = paths or default_paths()
    keys = _keys(paths)
    version = _read_value(keys["uninstall"], "DisplayVersion") if sys.platform == "win32" else None
    installed = os.path.isfile(paths.exe) and version is not None
    running = os.path.normcase(os.path.abspath(sys.executable)) == os.path.normcase(os.path.abspath(paths.exe))
    return InstallState(installed, version, running, paths.exe,
                        mrpack=_read_value(f"{keys['mrpack_progid']}\\shell\\open\\command") is not None,
                        protocol=_read_value(f"{keys['protocol']}\\shell\\open\\command") is not None,
                        desktop=os.path.isfile(paths.desktop_shortcut))


# --- Установка и удаление ---

def install(source_exe: str, desktop: bool = True, associate_mrpack: bool = True, protocol: bool = True,
            paths: Optional[InstallPaths] = None, make_shortcuts: bool = True) -> List[str]:
    """Устанавливает (или обновляет) программу. Возвращает список выполненных шагов для отчёта."""
    paths = paths or default_paths()
    keys = _keys(paths)
    done = []
    os.makedirs(paths.install_dir, exist_ok=True)
    if os.path.normcase(os.path.abspath(source_exe)) != os.path.normcase(os.path.abspath(paths.exe)):
        shutil.copy2(source_exe, paths.exe)
        done.append(f"Программа скопирована в {paths.install_dir}")
    exe = paths.exe
    if make_shortcuts:
        create_shortcut(paths.start_shortcut, exe)
        done.append("Ярлык в меню «Пуск»")
        if desktop:
            create_shortcut(paths.desktop_shortcut, exe)
            done.append("Ярлык на рабочем столе")
        elif os.path.exists(paths.desktop_shortcut):
            os.remove(paths.desktop_shortcut)
    size_kb = max(1, os.path.getsize(exe) // 1024) if os.path.isfile(exe) else 1
    _set_values(keys["uninstall"], {
        "DisplayName": APP_TITLE, "DisplayVersion": app_config.APP_VERSION, "Publisher": "TheMrEvgen",
        "DisplayIcon": f"{exe},0", "InstallLocation": paths.install_dir, "UninstallString": f'"{exe}" --uninstall',
        "QuietUninstallString": f'"{exe}" --uninstall --quiet', "NoModify": 1, "NoRepair": 1, "EstimatedSize": size_kb,
    })
    done.append("Пункт в «Установке и удалении программ»")
    if associate_mrpack:
        _set_values(keys["mrpack_progid"], {"": "Сборка Modrinth (.mrpack)"})
        _set_values(f"{keys['mrpack_progid']}\\DefaultIcon", {"": f"{exe},0"})
        _set_values(f"{keys['mrpack_progid']}\\shell\\open\\command", {"": f'"{exe}" "%1"'})
        _set_values(keys["mrpack_ext"], {MRPACK_PROGID: ""})
        if not _read_value(keys["mrpack_default"]):  # Не перехватываем .mrpack у другой программы без спроса
            _set_values(keys["mrpack_default"], {"": MRPACK_PROGID})
        done.append("Открытие .mrpack двойным щелчком")
    else:
        _delete_tree(keys["mrpack_progid"])
    if protocol:
        _set_values(keys["protocol"], {"": "URL:Minecraft Mod Updater", "URL Protocol": ""})
        _set_values(f"{keys['protocol']}\\DefaultIcon", {"": f"{exe},0"})
        _set_values(f"{keys['protocol']}\\shell\\open\\command", {"": f'"{exe}" "%1"'})
        done.append("Ссылки modupdater://")
    else:
        _delete_tree(keys["protocol"])
    _notify_shell()
    logging.info("Установка: " + "; ".join(done))
    return done


def uninstall(paths: Optional[InstallPaths] = None, remove_data: bool = False, schedule_exe_removal: bool = True) -> List[str]:
    """Удаляет ярлыки, записи реестра и (после выхода программы) папку установки. Моды не затрагиваются."""
    paths = paths or default_paths()
    keys = _keys(paths)
    done = []
    for shortcut in (paths.start_shortcut, paths.desktop_shortcut):
        if os.path.exists(shortcut):
            os.remove(shortcut)
            done.append(f"Удалён ярлык {os.path.basename(shortcut)}")
    if _read_value(keys["mrpack_default"]) == MRPACK_PROGID:  # Снимаем только своё значение по умолчанию
        try:
            winreg = _winreg()
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, keys["mrpack_default"], 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, "")
        except OSError:
            pass
    for key in ("mrpack_progid", "protocol", "uninstall"):
        _delete_tree(keys[key])
    try:
        winreg = _winreg()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, keys["mrpack_ext"], 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, MRPACK_PROGID)
    except OSError:
        pass
    done.append("Записи реестра удалены")
    _notify_shell()
    if remove_data:
        shutil.rmtree(app_config.CONFIG_DIR, ignore_errors=True)
        done.append("Настройки и кэш удалены")
    if schedule_exe_removal and os.path.isdir(paths.install_dir):
        # Запущенный .exe нельзя удалить сразу — удаляем папку через пару секунд после выхода
        subprocess.Popen(f'cmd /c ping 127.0.0.1 -n 3 >nul & rmdir /s /q "{paths.install_dir}"',
                         creationflags=CREATE_NO_WINDOW, close_fds=True)
        done.append("Папка программы будет удалена после выхода")
    logging.info("Удаление: " + "; ".join(done))
    return done
