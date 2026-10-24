"""Готовые сборки из каталога: вкладка «Сборки», окно установки, новая сборка Prism (в песочнице, не в настоящем Prism)."""
import json
import logging
import os
import shutil
import sys
import time

PROJECT, SP = sys.argv[1], sys.argv[2]
FAKE_APPDATA = f"{SP}/fake_appdata"
SHOTS = f"{SP}/shots/mp"
sys.path.insert(0, PROJECT)
logging.basicConfig(level=logging.WARNING)
shutil.rmtree(FAKE_APPDATA, ignore_errors=True)
os.makedirs(f"{FAKE_APPDATA}/PrismLauncher/instances")
import config as app_config  # noqa: E402

app_config.CACHE_FILE = f"{SP}/cache/mod_cache.db"
app_config.ICON_CACHE_DIR = f"{SP}/cache/icons96"
app_config.CONFIG_DIR = f"{SP}/fake_config"
app_config.load_config = lambda *a, **k: app_config._normalize({
    "theme": "dark", "current_profile": "Песочница",
    "profiles": {"Песочница": {**app_config.default_profile(), "mods_path": f"{SP}/game", "manual_mode": True,
                               "game_version": "1.21.1", "loader": "fabric"}},
    "settings": {"auto_check": False, "discover_launchers": False, "app_update_check": False}})
app_config.save_config = lambda *a, **k: None
os.makedirs(SHOTS, exist_ok=True)

import launchers  # noqa: E402

launchers._appdata = lambda: FAKE_APPDATA  # «Prism» — папка в песочнице
from PyQt6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()
import main_window as mw  # noqa: E402

w = mw.ModUpdaterApp()
w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
w.resize(1300, 840)
w.show()
w._play = lambda: None


def pump(sec=0.3):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 30)
        time.sleep(0.01)


def check(label, condition):
    print(("OK   " if condition else "FAIL ") + label, flush=True)


pump(2)
w._switch_page(mw.PAGE_CATALOG)
w.sidebar.set_page(mw.PAGE_CATALOG)
pump(1)
page = w.catalog_page
tabs = [b.text().strip() for b in page.tab_group.buttons()]
check(f"вкладки каталога: {tabs}", tabs[-1] == "Сборки")
page.tab_group.buttons()[-1].click()
page.compatible.setChecked(False)
page.search.setText("fabulously optimized")
page.refresh()
end = time.time() + 30
while time.time() < end and (page._loading or not page.hits):
    pump(0.3)
pump(1)
check(f"сборки найдены: {[h['title'] for h in page.hits[:3]]}", any("Fabulously" in h["title"] for h in page.hits))
w.grab().save(f"{SHOTS}/01_catalog.png")
hit = next(h for h in page.hits if "Fabulously" in h["title"])
state = {}


def drive():
    dialog = QApplication.activeModalWidget()
    if dialog is None or not hasattr(dialog, "plan"):
        QTimer.singleShot(150, drive)
        return
    if dialog.plan is None:
        QTimer.singleShot(200, drive)
        return
    if "info" not in state:
        state["info"] = dialog.info.text()
        state["prism_offered"] = dialog.prism_radio.isVisible() and dialog.prism_radio.isChecked()
        dialog.grab().save(f"{SHOTS}/02_dialog.png")
        dialog.install_btn.click()
    if dialog.isVisible():
        QTimer.singleShot(200, drive)


QTimer.singleShot(200, drive)
w._install_modpack(hit)
pump(1)
check(f"окно показало, что внутри: {state.get('info')}", "Minecraft" in state.get("info", ""))
check("предложена новая сборка Prism", state.get("prism_offered"))
instances = os.listdir(f"{FAKE_APPDATA}/PrismLauncher/instances")
check(f"создана сборка Prism: {instances}", len(instances) == 1)
instance = f"{FAKE_APPDATA}/PrismLauncher/instances/{instances[0]}"
pack = json.load(open(f"{instance}/mmc-pack.json", encoding="utf-8"))
uids = [c["uid"] for c in pack["components"]]
check(f"mmc-pack.json: {uids}", "net.minecraft" in uids and "org.lwjgl3" in uids and "net.fabricmc.fabric-loader" in uids)
mods = os.listdir(f"{instance}/minecraft/mods")
check(f"моды скачаны: {len(mods)}, настройки распакованы: {os.path.isdir(instance + '/minecraft/config')}",
      len(mods) > 10 and os.path.isdir(f"{instance}/minecraft/config"))
check(f"сборка в списке слева и открыта: {w.current_profile_name}", w.current_profile_name == instances[0]
      and w.profiles[instances[0]]["source"] == "prism")
w.grab().save(f"{SHOTS}/03_installed.png")
print("done")
