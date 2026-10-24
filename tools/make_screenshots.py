"""
Скриншоты для README: python tools/make_screenshots.py <рабочая папка ui_checks> [ru|en]
Нужны песочницы из tools/ui_checks (запустите run.py хотя бы раз) и интернет. Пути и имена — демонстрационные.
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = sys.argv[1]
LANG = sys.argv[2] if len(sys.argv) > 2 else "en"
OUT = os.path.join(ROOT, "docs", "screenshots")
os.environ["MODUPDATER_LANG"] = LANG
sys.path.insert(0, ROOT)
import config as app_config  # noqa: E402

app_config.CACHE_FILE = f"{WORK}/cache/mod_cache.db"
app_config.ICON_CACHE_DIR = f"{WORK}/cache/icons96"
PACKS = {"Survival 1.21.11": f"{WORK}/game2", "Optimized": f"{WORK}/game"}
app_config.load_config = lambda *a, **k: app_config._normalize({
    "theme": "dark", "current_profile": "Survival 1.21.11",
    "profiles": {name: {**app_config.default_profile(), "mods_path": path, "channel": "release", "source": "prism",
                        "manual_mode": True, "game_version": "1.21.11" if "Survival" in name else "1.21.1",
                        "loader": "fabric"} for name, path in PACKS.items()},
    "settings": {"auto_check": True, "discover_launchers": False, "app_update_check": False, "details_on_select": True,
                 "columns": ["description", "versions", "size", "date"], "language": LANG}})
app_config.save_config = lambda *a, **k: None
os.makedirs(OUT, exist_ok=True)

from PyQt6.QtCore import QEventLoop, Qt  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()
import main_window as mw  # noqa: E402

w = mw.ModUpdaterApp()
w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
w.resize(1400, 860)
w.show()


def pump(sec=0.4):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 30)
        time.sleep(0.01)


def wait_idle(timeout=120):
    pump(1)
    end = time.time() + timeout
    while time.time() < end and w._busy is not None:
        pump(0.3)
    pump(1.5)


def demo_path():
    w.path_label.setFullText(r"C:\Games\PrismLauncher\instances\Survival\minecraft")  # Без настоящих путей


def save(name):
    for toast in list(w.toasts.toasts):
        toast.hide()
    pump(0.3)
    w.grab().save(os.path.join(OUT, f"{name}{'' if LANG == 'en' else '_' + LANG}.png"))
    print("saved", name)


wait_idle()
demo_path()
save("mods")
entry = next(e for e in w._count_items if e.get("name") == "Sodium")
w._select_entry(entry)
pump(3)
demo_path()
save("details")
w._close_details()

w._switch_page(mw.PAGE_CATALOG)
w.sidebar.set_page(mw.PAGE_CATALOG)
pump(1)
page = w.catalog_page
page.tab_group.buttons()[-1].click()  # Сборки
page.compatible.setChecked(False)
page.search.setText("optimized")
page.refresh()
end = time.time() + 30
while time.time() < end and (page._loading or not page.hits):
    pump(0.3)
row = next((i for i, h in enumerate(page.hits) if "Fabulously" in h["title"]), 0)
page.list.setCurrentRow(row)
page.details.show_hit(page.hits[row])
pump(6)
save("catalog")

w._switch_page(mw.PAGE_STATS)
w.sidebar.set_page(mw.PAGE_STATS)
pump(1.5)
save("statistics")

w._switch_page(mw.PAGE_SETTINGS)
w.sidebar.set_page(mw.PAGE_SETTINGS)
pump(1)
save("settings")
