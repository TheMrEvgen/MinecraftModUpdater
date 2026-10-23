"""Прогон новых возможностей на копии сборки: группы, закрепление, выключение, вкладки, каталог, палитра,
список изменений, обновление со снимком, история, переход на другую версию."""
import logging
import os
import sys
import time

PROJECT, SP = sys.argv[1], sys.argv[2]
GAME = f"{SP}/game2"
SHOTS = f"{SP}/shots/f"
sys.path.insert(0, PROJECT)
logging.basicConfig(level=logging.WARNING)
import config as app_config  # noqa: E402

app_config.CACHE_FILE = f"{SP}/cache/mod_cache.db"
app_config.ICON_CACHE_DIR = f"{SP}/cache/icons96"
app_config.load_config = lambda *a, **k: app_config._normalize({
    "theme": "dark", "current_profile": "Копия сборки",
    "profiles": {"Копия сборки": {**app_config.default_profile(), "mods_path": GAME, "channel": "release",
                                  "manual_mode": True, "game_version": "1.21.11", "loader": "fabric"}},
    "settings": {"auto_check": True, "discover_launchers": False}})
app_config.save_config = lambda *a, **k: None
os.makedirs(SHOTS, exist_ok=True)

from PyQt6.QtCore import QEventLoop, Qt  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()
import main_window as mw  # noqa: E402
import ui.dialogs as dialogs  # noqa: E402
from content import MODS, RESOURCEPACKS, SHADERS  # noqa: E402
from ui.mod_list import entry_at, meta_at  # noqa: E402

log = []
mw.confirm = lambda *a, **k: (log.append(("confirm", a[1])), True)[1]
mw.ask_choice = lambda parent, icon, title, text, buttons, **k: (log.append(("choice", title)), 0)[1]
mw.show_message = lambda parent, icon, title, text: log.append(("msg", title, text[:100]))
toasts = []

w = mw.ModUpdaterApp()
orig_show = w.toasts.show
w.toasts.show = lambda text, kind="info", action_text=None, action=None, timeout=5000: (
    toasts.append((kind, text)), orig_show(text, kind, action_text, action, timeout))
w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
w.resize(1440, 900)
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
    (widget or w).grab().save(f"{SHOTS}/{name}.png")


wait_idle()
print("items", len(w._count_items), "updates", len(w._bulk_candidates()), "toasts", toasts[-2:])
shot("01_groups_dark")

# Закрепление и выключение
updatable = [e for e in w._count_items if e.get("download_url")]
pinned_entry, disabled_entry = updatable[0], updatable[1]
w._toggle_pin(pinned_entry)
w._toggle_disabled([disabled_entry])
pump(0.5)
print("pinned", pinned_entry["name"], pinned_entry.get("_pinned"), "| disabled", disabled_entry["name"],
      disabled_entry.get("disabled"), os.path.basename(disabled_entry["filepath"]))
print("bulk now", len(w._bulk_candidates()), "disabled card", w.cards["disabled"].count, w.cards["disabled"].isVisible())
w._select_entry(pinned_entry)
w._open_current_details()
pump(0.6)
shot("02_pinned_disabled")

# Метки
print("tags", [w.tag_combo.itemText(i) for i in range(min(6, w.tag_combo.count()))])
w.tag_combo.setCurrentIndex(1)
pump(0.4)
print("tag filter rows", w.proxy.rowCount())
w.tag_combo.setCurrentIndex(0)
w._close_details()

# Дерево с анимацией
w._set_view_mode("tree")
pump(0.3)
for r in range(w.proxy.rowCount()):
    if meta_at(w.proxy.index(r, 0)).get("children"):
        w._set_expanded(entry_at(w.proxy.index(r, 0)), True)
        pump(0.06)
        shot("03_tree_anim_mid")
        break
pump(0.5)
print("anim left", w.delegate.anim)
shot("04_tree")
w._set_view_mode("flat")

# Сортировки
w.sort_combo.setCurrentIndex(3)
pump(0.3)
first = entry_at(w.proxy.index(0, 0))
print("largest first:", first["name"], first.get("size"))
w.sort_combo.setCurrentIndex(0)

# Вкладки шейдеров и ресурспаков
w._set_content(SHADERS)
wait_idle()
print("shaders", [(e["name"], str(e["status"])) for e in w._count_items])
shot("05_shaders")
w._set_content(RESOURCEPACKS)
wait_idle()
print("resourcepacks", [(e["name"], str(e["status"])) for e in w._count_items])
shot("06_resourcepacks")
w._set_content(MODS)
pump(0.5)

# Палитра команд
w.palette.open()
w.palette.input.setText("тём")
pump(0.3)
print("palette top:", [c.title for c in w.palette.visible[:3]])
shot("07_palette")
w.palette.hide()

# Каталог
w._switch_page(mw.PAGE_CATALOG)
wait_idle()
for _ in range(100):
    pump(0.1)
    if w.catalog_page.hits:
        break
pump(1.5)
print("catalog hits", len(w.catalog_page.hits), w.catalog_page.status.text())
shot("08_catalog")
w.catalog_page.search.setText("appleskin")
pump(1.5)
wait_idle()
hit = next((h for h in w.catalog_page.hits if w.catalog_page.install_state(h) == "available"), None)
print("install from catalog:", hit and hit["title"])
if hit:
    w._install_from_catalog(hit)
    wait_idle()
    for _ in range(100):
        pump(0.1)
        if not w._catalog_installing and w._busy is None:
            break
    wait_idle()
print("catalog toasts", toasts[-3:])
shot("09_catalog_installed")

# Окно «Что нового»
w._switch_page(mw.PAGE_MODS)
wait_idle()
entries = w._bulk_candidates()[:6]
review = dialogs.UpdateReviewDialog(w, w.tokens, entries, w._icon_for_entry,
                                    lambda cb: w._run_task(mw.collect_changelogs, (entries, w.game_version, w.loader, MODS),
                                                           cb, lambda e: cb({}), None))
review.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
review.show()
for _ in range(150):
    pump(0.1)
    if review.changelogs is not None:
        break
review.list.setCurrentRow(0)
pump(0.5)
print("changelogs loaded:", {k: len(v) for k, v in (review.changelogs or {}).items()})
shot("10_review", review)
review.close()

# Обновить все (окно подтверждаем автоматически) — со снимком
dialogs_exec = mw.UpdateReviewDialog.exec
mw.UpdateReviewDialog.exec = lambda self: 1
before = len(os.listdir(f"{GAME}/mods"))
w.update_all()
wait_idle(600)
mw.UpdateReviewDialog.exec = dialogs_exec
print("after update: bulk", len(w._bulk_candidates()), "files", before, "->", len([f for f in os.listdir(f"{GAME}/mods") if f.endswith(('.jar', '.disabled'))]))
print("pinned still old:", pinned_entry["name"], pinned_entry.get("current_version"), pinned_entry.get("new_version"))
shot("11_after_update")
w._switch_page(mw.PAGE_HISTORY)
pump(0.6)
from updater import list_snapshots  # noqa: E402
print("snapshots", [(s["reason"], len(s["files"])) for s in list_snapshots(f"{GAME}/mods")])
shot("12_history")

# Переход на другую версию: только проверка в окне
w._switch_page(mw.PAGE_MODS)
mig = dialogs.MigrationDialog(w, w.tokens, [e for e in w._count_items if e.get("filepath")], w.game_version, w.loader,
                              w._channel(), w._run_task, MODS)
mig.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
mig.show()
for _ in range(100):
    pump(0.1)
    if mig.version_combo.count():
        break
mig.version_combo.setCurrentText("1.21.10")
mig._check()
for _ in range(200):
    pump(0.1)
    if mig.results:
        break
pump(0.4)
print("migration:", mig.summary.text(), "ok enabled", mig.ok.isEnabled())
shot("13_migration", mig)
mig.close()

# Светлая тема
w._on_theme_selected("light")
pump(0.6)
shot("14_light")
w._switch_page(mw.PAGE_CATALOG)
pump(1.0)
shot("15_catalog_light")
print("log", log[-6:])
w.icon_loader.shutdown()
print("done")
