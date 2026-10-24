"""Датапаки: вкладка с выбором мира, проверка обновлений (Modrinth), пустое состояние без миров."""
import logging
import os
import shutil
import sys
import time

PROJECT, SP = sys.argv[1], sys.argv[2]
GAME = f"{SP}/game_dp"
SHOTS = f"{SP}/shots/dp"
sys.path.insert(0, PROJECT)
logging.basicConfig(level=logging.WARNING)
import requests  # noqa: E402

# Песочница: два мира, в «Выживании» — старая версия датапака Terralith для 1.21.1
session = requests.Session()
session.headers["User-Agent"] = "TheMrEvgen/MinecraftModUpdater-ui-checks"
shutil.rmtree(GAME, ignore_errors=True)
for world in ("Выживание", "Креатив"):
    os.makedirs(f"{GAME}/saves/{world}/datapacks")
    with open(f"{GAME}/saves/{world}/level.dat", "wb") as f:
        f.write(b"\x0a\x00\x00\x00")
    time.sleep(0.05)
os.utime(f"{GAME}/saves/Выживание/level.dat")  # Последний сыгранный — «Выживание»
os.makedirs(f"{GAME}/mods")
versions = session.get("https://api.modrinth.com/v2/project/terralith/version", timeout=30,
                       params={"loaders": '["datapack"]', "game_versions": '["1.21.1"]'}).json()
old = next(f for f in versions[-1]["files"] if f["primary"])
cached = f"{SP}/downloads/datapacks/{old['filename']}"
if not os.path.isfile(cached):
    os.makedirs(os.path.dirname(cached), exist_ok=True)
    with open(cached, "wb") as f:
        f.write(session.get(old["url"], timeout=120).content)
shutil.copy(cached, f"{GAME}/saves/Выживание/datapacks/{old['filename']}")

import config as app_config  # noqa: E402

app_config.CACHE_FILE = f"{SP}/cache/mod_cache.db"
app_config.ICON_CACHE_DIR = f"{SP}/cache/icons96"
app_config.load_config = lambda *a, **k: app_config._normalize({
    "theme": "dark", "current_profile": "Датапаки",
    "profiles": {"Датапаки": {**app_config.default_profile(), "mods_path": GAME, "channel": "release",
                              "manual_mode": True, "game_version": "1.21.1", "loader": "fabric"}},
    "settings": {"auto_check": True, "discover_launchers": False, "app_update_check": False}})
app_config.save_config = lambda *a, **k: None
os.makedirs(SHOTS, exist_ok=True)

from PyQt6.QtCore import QEventLoop, Qt  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()
import main_window as mw  # noqa: E402
from content import DATAPACKS  # noqa: E402
from enums import ModStatus  # noqa: E402

w = mw.ModUpdaterApp()
w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
w.resize(1300, 820)
w.show()


def pump(sec=0.3):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 30)
        time.sleep(0.01)


def wait_idle(timeout=120):
    pump(1)
    end = time.time() + timeout
    while time.time() < end and w._busy is not None:
        pump(0.3)
    pump(0.8)


def check(label, condition):
    print(("OK   " if condition else "FAIL ") + label, flush=True)


wait_idle()
check("вкладка «Датапаки» есть", len(w.content_group.buttons()) == 4)
w._set_content(DATAPACKS)
wait_idle()
w._on_check_clicked()
wait_idle()
check(f"плашка мира: «{w.world_chip.text()}»", w.world_chip.isVisible() and "Выживание" in w.world_chip.text())
check(f"папка вкладки — датапаки мира: {w.mods_path}", w.mods_path.replace("\\", "/").endswith("saves/Выживание/datapacks"))
entry = next((e for e in w._count_items if e.get("filepath")), None)
check(f"датапак опознан и найдено обновление: {entry and (entry.get('name'), entry.get('current_version'), entry.get('new_version'))}",
      bool(entry) and entry.get("status") == ModStatus.UPDATE_AVAILABLE)
w.grab().save(f"{SHOTS}/01_datapacks.png")

w._set_datapack_world("Креатив")
wait_idle()
check("другой мир — свой список (пустой)", not [e for e in w._count_items if e.get("filepath")]
      and w.mods_path.replace("\\", "/").endswith("saves/Креатив/datapacks"))
w._set_datapack_world("Выживание")
wait_idle()
check("вернулись в «Выживание» — датапак снова в списке", any(e.get("filepath") for e in w._count_items))

shutil.rmtree(f"{GAME}/saves")
w._load_content()
wait_idle()
check(f"без миров — понятное пустое состояние: «{w.empty_state.title.text()}»", "Миров" in w.empty_state.title.text())
w.grab().save(f"{SHOTS}/02_no_worlds.png")
print("done")
