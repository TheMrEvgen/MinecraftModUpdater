"""Аудит внешнего вида: все страницы и окна при минимальном и обычном размере окна."""
import logging
import os
import sys
import time

PROJECT, SP = sys.argv[1], sys.argv[2]
GAME = f"{SP}/game2"
SHOTS = f"{SP}/shots/a"
sys.path.insert(0, PROJECT)
logging.basicConfig(level=logging.WARNING)
import config as app_config  # noqa: E402

app_config.CACHE_FILE = f"{SP}/cache/mod_cache.db"
app_config.ICON_CACHE_DIR = f"{SP}/cache/icons96"
app_config.load_config = lambda *a, **k: app_config._normalize({
    "theme": "dark", "current_profile": "Копия сборки",
    "profiles": {"Копия сборки": {**app_config.default_profile(), "mods_path": GAME, "channel": "release",
                                  "manual_mode": True, "game_version": "1.21.11", "loader": "fabric"},
                 "Сервер": {**app_config.default_profile(), "mods_path": GAME, "group": "Серверы", "color": "#4c8dff",
                            "icon": "shield"}},
    "settings": {"auto_check": True, "discover_launchers": False, "columns": ["description", "versions", "size", "date"]}})
app_config.save_config = lambda *a, **k: None
os.makedirs(SHOTS, exist_ok=True)

from PyQt6.QtCore import QEventLoop, Qt  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()
import main_window as mw  # noqa: E402
from ui.gallery import ImageViewer  # noqa: E402

w = mw.ModUpdaterApp()
w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
w.resize(1040, 660)
w.show()


def pump(sec=0.3):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 30)
        time.sleep(0.01)


def wait_idle(timeout=240):
    pump(0.4)
    end = time.time() + timeout
    while (w._busy is not None or any(t.isRunning() for t in w._task_workers)) and time.time() < end:
        pump(0.05)
    pump(0.8)


def shot(name, widget=None):
    for toast in list(w.toasts.toasts):
        toast.dismiss()
    pump(0.2)
    (widget or w).grab().save(f"{SHOTS}/{name}.png")


wait_idle()
sodium = next(e for e in w._count_items if e["name"] == "Sodium")
w._select_entry(sodium)
w._open_current_details()
pump(1.5)
shot("01_mods_small")
for area in w.details.findChildren(mw.QWidget.__mro__[0].__class__.__base__ if False else type(w.details.gallery.scroll)):
    pass
w.details.layout_.parentWidget().parentWidget().parentWidget().verticalScrollBar().setValue(600)
pump(1.5)
shot("02_details_scrolled")
if w.details.gallery and w.details.gallery.items:
    w.details.gallery._step(1)
    pump(0.6)
    shot("03_gallery_stepped", w.details)
    viewer = ImageViewer(w, w.full_loader, w.details.gallery.items, 0, w.tokens)
    viewer.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    viewer.resize(1100, 700)
    viewer.show()
    for _ in range(80):
        pump(0.1)
        if viewer._pixmap:
            break
    shot("04_viewer", viewer)
    viewer.go(1)
    for _ in range(80):
        pump(0.1)
        if viewer._pixmap:
            break
    print("viewer:", viewer.counter.text())
    shot("05_viewer_next", viewer)
    viewer.close()
else:
    print("no gallery for Sodium")

w._switch_page(mw.PAGE_CATALOG)
for _ in range(150):
    pump(0.1)
    if w.catalog_page.hits:
        break
pump(1)
w.catalog_page.list.setCurrentRow(0)
for _ in range(80):
    pump(0.1)
    if w.catalog_page.details.versions:
        break
pump(2)
shot("06_catalog_small")
w._switch_page(mw.PAGE_STATS)
pump(1)
shot("07_stats_small")
w._switch_page(mw.PAGE_HISTORY)
pump(0.5)
shot("08_history_small")
w._switch_page(mw.PAGE_SETTINGS)
pump(0.5)
shot("09_settings_small")
w._switch_page(mw.PAGE_LOG)
pump(0.3)
shot("10_log_small")
w._switch_page(mw.PAGE_MODS)
w._close_details()
w.resize(1440, 900)
pump(0.8)
shot("11_mods_wide")
w._on_theme_selected("light")
w._switch_page(mw.PAGE_CATALOG)
pump(1.5)
shot("12_catalog_light")
for loader in (w.icon_loader, w.thumb_loader, w.full_loader):
    loader.shutdown()
print("done")
