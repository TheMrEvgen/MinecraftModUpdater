"""«Моды во всех сборках»: поиск по всем сборкам и объединение одинаковых файлов только по кнопке."""
import logging
import os
import shutil
import sys
import time

PROJECT, SP = sys.argv[1], sys.argv[2]
ROOT = f"{SP}/all_packs"
SHOTS = f"{SP}/shots/ap"
sys.path.insert(0, PROJECT)
logging.basicConfig(level=logging.WARNING)

# Три сборки: «Копия» — те же моды, что в «Песочнице» (как клон), «Большая» — свои
shutil.rmtree(ROOT, ignore_errors=True)
for name, source in (("Песочница", f"{SP}/game/mods"), ("Копия", f"{SP}/game/mods"), ("Большая", f"{SP}/game2/mods")):
    os.makedirs(f"{ROOT}/{name}/mods")
    for file in os.listdir(source):
        if file.endswith(".jar"):
            shutil.copy(os.path.join(source, file), f"{ROOT}/{name}/mods/{file}")

import config as app_config  # noqa: E402

app_config.CACHE_FILE = f"{SP}/cache/mod_cache.db"
app_config.ICON_CACHE_DIR = f"{SP}/cache/icons96"
app_config.load_config = lambda *a, **k: app_config._normalize({
    "theme": "dark", "current_profile": "Песочница",
    "profiles": {name: {**app_config.default_profile(), "mods_path": f"{ROOT}/{name}", "manual_mode": True,
                        "game_version": "1.21.1", "loader": "fabric"} for name in ("Песочница", "Копия", "Большая")},
    "settings": {"auto_check": False, "discover_launchers": False, "app_update_check": False}})
app_config.save_config = lambda *a, **k: None
os.makedirs(SHOTS, exist_ok=True)

from PyQt6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()
import main_window as mw  # noqa: E402
import ui.all_packs_dialog as apd  # noqa: E402

apd.confirm = lambda *a, **k: True
w = mw.ModUpdaterApp()
w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
w.show()


def pump(sec=0.3):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 30)
        time.sleep(0.01)


def check(label, condition):
    print(("OK   " if condition else "FAIL ") + label, flush=True)


pump(2)
state = {}


def drive():
    dialog = QApplication.activeModalWidget()
    if dialog is None or not hasattr(dialog, "tree"):
        QTimer.singleShot(100, drive)
        return
    if not dialog.rows:
        QTimer.singleShot(200, drive)
        return
    state["summary"] = dialog.summary.text()
    state["space"] = dialog.space_title.text() if dialog.space_card.isVisible() else ""
    dialog.search.setText("sodium")
    pump(0.6)
    state["found"] = [dialog.tree.topLevelItem(i).text(0) for i in range(dialog.tree.topLevelItemCount())]
    dialog.grab().save(f"{SHOTS}/01_search.png")
    files = [r["path"] for r in dialog.rows if r["pack"] in ("Песочница", "Копия")]
    state["before"] = len({os.stat(p).st_ino for p in files})
    dialog.link_btn.click()
    end = time.time() + 20
    while time.time() < end and dialog.groups:
        pump(0.2)
    pump(0.5)
    state["after"] = len({os.stat(p).st_ino for p in files})
    state["result"] = dialog.space_title.text()
    dialog.grab().save(f"{SHOTS}/02_linked.png")
    dialog.accept()


QTimer.singleShot(200, drive)
w._show_all_packs()
pump(0.3)
check(f"сводка: {state.get('summary')}", "Сборок: 3" in state.get("summary", ""))
check(f"найдено одинаковых: {state.get('space')}", "можно освободить" in state.get("space", ""))
check(f"поиск «sodium»: {state.get('found')}", (state.get("found") or [""])[0].startswith("Sodium") and "3 сборки" in state["found"][0])
check(f"объединено по кнопке: физических файлов {state.get('before')} → {state.get('after')} ({state.get('result')})",
      state.get("after", 0) * 2 == state.get("before", -1))
mods = f"{ROOT}/Песочница/mods"
check("файлы на месте и читаются", all(os.path.getsize(os.path.join(mods, f)) > 0 for f in os.listdir(mods)))
print("done")
