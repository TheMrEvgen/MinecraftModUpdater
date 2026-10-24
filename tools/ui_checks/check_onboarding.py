"""Мастер первого запуска, сворачиваемая боковая панель и поиск по настройкам."""
import logging
import os
import sys
import time

PROJECT, SP = sys.argv[1], sys.argv[2]
SHOTS = f"{SP}/shots/o"
sys.path.insert(0, PROJECT)
logging.basicConfig(level=logging.WARNING)
import config as app_config  # noqa: E402

app_config.CACHE_FILE = f"{SP}/cache/mod_cache.db"
app_config.ICON_CACHE_DIR = f"{SP}/cache/icons96"
app_config.load_config = lambda *a, **k: app_config._normalize({
    "theme": "dark", "current_profile": "Сборка",
    "profiles": {"Сборка": {**app_config.default_profile(), "mods_path": f"{SP}/game", "game_version": "1.21.1",
                            "loader": "fabric", "manual_mode": True},
                 "1.21.11": {**app_config.default_profile(), "mods_path": f"{SP}/game2"}},
    "settings": {"auto_check": False, "discover_launchers": False, "app_update_check": False}})
app_config.save_config = lambda *a, **k: None
os.makedirs(SHOTS, exist_ok=True)

from PyQt6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()
import main_window as mw  # noqa: E402

w = mw.ModUpdaterApp()
w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
w.resize(1300, 820)
w.show()


def pump(sec=0.3):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 30)
        time.sleep(0.01)


def check(label, condition):
    print(("OK   " if condition else "FAIL ") + label, flush=True)


pump(2)
# --- Мастер первого запуска ---
check("у существующих пользователей мастер не показывается", not w._first_run or w.settings.get("onboarding_done"))
w._first_run = True
steps = []


def drive():
    dialog = QApplication.activeModalWidget()
    if dialog is None or not hasattr(dialog, "pages"):
        QTimer.singleShot(100, drive)
        return
    index = dialog.pages.currentIndex()
    steps.append(dialog.step_label.text())
    dialog.grab().save(f"{SHOTS}/wizard_{index + 1}.png")
    if index == 0:
        dialog.theme_group.buttons()[2].setChecked(True)  # Светлая
    dialog.next_btn.click()
    if dialog.isVisible():
        QTimer.singleShot(150, drive)


QTimer.singleShot(200, drive)
w._maybe_onboarding()
pump(0.5)
check(f"мастер прошёл 3 шага: {steps}", len(steps) == 3)
check(f"тема из мастера применена: {w.config_theme}", w.config_theme == "light")
check("мастер больше не покажется", w.settings.get("onboarding_done") and w.settings.get("install_offered"))
w._on_theme_selected("dark")

# --- Боковая панель ---
w.sidebar.collapse_btn.click()
pump(0.5)
check(f"панель свёрнута кнопкой: {w.sidebar.width()} px", w.sidebar.width() == w.sidebar.NARROW and w.settings["sidebar_collapsed"])
w.grab().save(f"{SHOTS}/sidebar_collapsed.png")
w.sidebar.collapse_btn.click()
pump(0.3)
w.resize(1060, 720)
pump(0.5)
check(f"в узком окне свёрнута сама: {w.sidebar.width()} px", w.sidebar.width() == w.sidebar.NARROW)
w.resize(1300, 820)
pump(0.5)
check(f"в широком снова развёрнута: {w.sidebar.width()} px", w.sidebar.width() == w.sidebar.WIDE)

# --- Поиск по настройкам ---
w._switch_page(mw.PAGE_SETTINGS)
w.sidebar.set_page(mw.PAGE_SETTINGS)
pump(0.8)
page = w.settings_page
check(f"разделов в навигации: {page.nav_list.count()}", page.nav_list.count() >= 10)
page.settings_search.setText("трей")
pump(0.3)
shown = [page.nav_list.item(i).text() for i in range(page.nav_list.count()) if not page.nav_list.item(i).isHidden()]
check(f"поиск «трей» → {shown}", shown == ["Работа в фоне"])
w.grab().save(f"{SHOTS}/settings_search.png")
page.settings_search.setText("")
print("done")
