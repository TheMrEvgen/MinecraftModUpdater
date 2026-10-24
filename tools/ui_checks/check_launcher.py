"""Сборка Prism Launcher: обновление загрузчика и версии игры прямо в mmc-pack.json, отмена из уведомления."""
import json
import logging
import os
import shutil
import sys
import time

PROJECT, SP = sys.argv[1], sys.argv[2]
INSTANCE = f"{SP}/prism_instance"
GAME = f"{INSTANCE}/minecraft"
sys.path.insert(0, PROJECT)
logging.basicConfig(level=logging.WARNING)

# Сборка в стиле Prism: Fabric-загрузчик заведомо старый, чтобы было что обновлять
shutil.rmtree(INSTANCE, ignore_errors=True)
os.makedirs(f"{GAME}/mods")
PACK = {"formatVersion": 1, "components": [
    {"uid": "org.lwjgl3", "version": "3.3.3", "cachedVersion": "3.3.3", "dependencyOnly": True, "cachedName": "LWJGL 3"},
    {"uid": "net.minecraft", "version": "1.21.1", "cachedVersion": "1.21.1", "important": True, "cachedName": "Minecraft",
     "cachedRequires": [{"uid": "org.lwjgl3", "suggests": "3.3.3"}]},
    {"uid": "net.fabricmc.intermediary", "version": "1.21.1", "cachedVersion": "1.21.1", "dependencyOnly": True},
    {"uid": "net.fabricmc.fabric-loader", "version": "0.16.0", "cachedVersion": "0.16.0",
     "cachedRequires": [{"uid": "net.fabricmc.intermediary"}]}]}
with open(f"{INSTANCE}/mmc-pack.json", "w", encoding="utf-8") as f:
    json.dump(PACK, f, indent=4)

import config as app_config  # noqa: E402

app_config.CACHE_FILE = f"{SP}/cache/mod_cache.db"
app_config.ICON_CACHE_DIR = f"{SP}/cache/icons96"
app_config.load_config = lambda *a, **k: app_config._normalize({
    "theme": "dark", "current_profile": "Prism",
    "profiles": {"Prism": {**app_config.default_profile(), "mods_path": GAME, "source": "prism", "manual_mode": True,
                           "game_version": "1.21.1", "loader": "fabric", "loader_version": "0.16.0"}},
    "settings": {"auto_check": False, "discover_launchers": False, "app_update_check": False}})
app_config.save_config = lambda *a, **k: None

from PyQt6.QtCore import QEventLoop, Qt  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()
import launcher_pack  # noqa: E402
import main_window as mw  # noqa: E402
from window_patch import patch_window  # noqa: E402

launcher_pack.launcher_running = lambda: False
patch_window(confirm=lambda *a, **k: True, show_message=lambda *a, **k: None)
w = mw.ModUpdaterApp()
w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
w.show()
toasts = []
original = w.toasts.show
w.toasts.show = lambda text, kind="info", action_text=None, action=None, timeout=5000: (
    toasts.append((text, action)), original(text, kind, action_text, action, timeout))


def pump(sec=0.3):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 30)
        time.sleep(0.01)


def check(label, condition):
    print(("OK   " if condition else "FAIL ") + label, flush=True)


def pack():
    with open(f"{INSTANCE}/mmc-pack.json", encoding="utf-8") as f:
        return {c["uid"]: c for c in json.load(f)["components"]}


end = time.time() + 30
while time.time() < end and not w.latest_loader_version:
    pump(0.3)
check(f"найдена новая версия загрузчика: {w.current_loader_version} → {w.latest_loader_version}", bool(w.latest_loader_version))
new = w.latest_loader_version
w._on_loader_chip_clicked()
pump(0.5)
check(f"загрузчик обновлён в mmc-pack.json: {pack()['net.fabricmc.fabric-loader']['version']}",
      pack()["net.fabricmc.fabric-loader"]["version"] == new and w.current_loader_version == new)
undo = next(action for text, action in toasts if action and new in text)
undo()
pump(0.5)
check("«Отменить» вернул прежнюю версию", pack()["net.fabricmc.fabric-loader"]["version"] == "0.16.0")

# Смена версии игры (как в конце «Перейти на другую версию Minecraft»)
w._migrate_launcher_pack("1.21.4", "1.21.1", "0.16.0")
end = time.time() + 40
while time.time() < end and pack()["net.minecraft"]["version"] == "1.21.1":
    pump(0.3)
pump(0.5)
result = pack()
check(f"Minecraft в сборке: {result['net.minecraft']['version']}, intermediary: {result['net.fabricmc.intermediary']['version']}",
      result["net.minecraft"]["version"] == "1.21.4" and result["net.fabricmc.intermediary"]["version"] == "1.21.4")
check(f"LWJGL по справочнику Prism: {result['org.lwjgl3']['version']}",
      result["org.lwjgl3"]["version"] == result["net.minecraft"]["cachedRequires"][0].get("suggests"))
check(f"загрузчик под новую версию: {result['net.fabricmc.fabric-loader']['version']}",
      result["net.fabricmc.fabric-loader"]["version"] != "0.16.0" and w.game_version == "1.21.4")
check(f"копии файла сборки сохранены: {len([f for f in os.listdir(INSTANCE) if f.endswith('.bak')])}",
      len([f for f in os.listdir(INSTANCE) if f.endswith(".bak")]) == 2)
print("done")
