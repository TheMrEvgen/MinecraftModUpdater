"""Игра: «Играть» с имитацией игры, наблюдение, разбор вылета и автоматический поиск проблемного мода."""
import logging
import os
import sys
import time

PROJECT, SP = sys.argv[1], sys.argv[2]
GAME = f"{SP}/game"
FAKE = f"{SP}/fakegame/fake_game.py"
sys.path.insert(0, PROJECT)
logging.basicConfig(level=logging.INFO, format="%(message)s")
import config as app_config  # noqa: E402

app_config.CACHE_FILE = f"{SP}/cache/mod_cache.db"
app_config.ICON_CACHE_DIR = f"{SP}/cache/icons96"
culprit = {"name": "modmenu"}


def profile():
    return {**app_config.default_profile(), "mods_path": GAME, "manual_mode": True, "game_version": "1.21.1",
            "loader": "fabric", "launch_command": f'"{sys.executable}" "{FAKE}" "{GAME}" {culprit["name"]}'}


app_config.load_config = lambda *a, **k: app_config._normalize({
    "theme": "dark", "current_profile": "T", "profiles": {"T": profile()},
    "settings": {"auto_check": False, "discover_launchers": False, "auto_bisect_timeout": 2}})
app_config.save_config = lambda *a, **k: None

from PyQt6.QtCore import QEventLoop, Qt  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()
import main_window as mw  # noqa: E402
from ui.tools_dialogs import CrashDialog  # noqa: E402
from window_patch import patch_window  # noqa: E402

opened = []
patch_window(confirm=lambda *a, **k: True, show_message=lambda *a, **k: None,
             ask_choice=lambda *a, **k: 0)  # «Автоматически»
CrashDialog.exec = lambda self: opened.append(self.file_combo.currentText()) or 0

w = mw.ModUpdaterApp()
w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
w.show()


def pump(sec=0.3):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 30)
        time.sleep(0.02)


pump(3)
while w._busy:
    pump(0.2)
print("Кнопка:", w.play_btn.text(), w.play_btn.isEnabled(), "|", w.play_btn.toolTip())

# 1. «Играть» с «виновным» модом — игра вылетает, разбор открывается сам
w._play()
for _ in range(100):
    pump(0.2)
    if opened:
        break
print("Плашка «Игра запущена» показывалась:", w._game_running or "см. выше", "| разбор открыт:", opened)

# 2. Автоматический поиск
pump(2)
w._start_bisect()
start = time.time()
while w._bisect_session() and not w._bisect_session().done and time.time() - start < 120:
    pump(0.3)
session = w._bisect_session()
print(f"Автопоиск: {time.time() - start:.0f} с, виновник = {session and session.culprit}")
w._finish_bisect(True)
pump(0.5)
print("Выключены после завершения:", [os.path.basename(e["filepath"]) for e in w._installed_mods() if e.get("disabled")])
w._finish_bisect(False) if w._bisect_session() else None
# Вернуть песочницу: включить всё обратно
w._set_disabled_names(set())
print("Файлы:", sorted(f for f in os.listdir(f"{GAME}/mods") if f.endswith((".jar", ".disabled"))))

# 3. История запусков и подсказка откатить изменение модов, после которого игра вылетает
import shutil  # noqa: E402
from updater import UpdateTransaction  # noqa: E402

notes = []
original_toast = w.toasts.show
w.toasts.show = lambda text, kind="info", action_text=None, action=None, timeout=5000: (
    notes.append((text, action)), original_toast(text, kind, action_text, action, timeout))


def check(label, condition):
    print(("OK   " if condition else "FAIL ") + label, flush=True)


def set_culprit(name):
    w._profile()["launch_command"] = f'"{sys.executable}" "{FAKE}" "{GAME}" {name}'


def launch_and_wait(timeout=40):
    before = len(w._profile().get("launches") or [])
    w._play()
    end = time.time() + timeout
    while time.time() < end and len(w._profile().get("launches") or []) == before:
        pump(0.3)
    pump(0.5)
    return (w._profile().get("launches") or [None])[-1]


set_culprit("no-such-mod")
good = launch_and_wait()
check(f"удачный запуск записан: {good}", bool(good) and good["menu"] and not good["crashed"])
check("время загрузки посчитано по логу (~2 с)", bool(good) and good.get("load") is not None and 1 <= good["load"] <= 4)

time.sleep(1.5)  # Операция должна быть позже удачного запуска (время в журнале — с точностью до секунды)
mods_dir = f"{GAME}/mods"
source_jar = next(f for f in os.listdir(mods_dir) if f.endswith(".jar"))
shutil.copy(os.path.join(mods_dir, source_jar), os.path.join(mods_dir, "broken-update-1.0.jar"))
UpdateTransaction(mods_dir, "update").record_installed("broken-update-1.0.jar")

set_culprit("broken-update")
bad = launch_and_wait()
check("вылет после изменения модов записан", bool(bad) and bad["crashed"])
suggestion = next(((text, action) for text, action in notes if "Откатить это изменение" in text), None)
check(f"предложено откатить изменение: {suggestion[0] if suggestion else None}",
      bool(suggestion) and "broken-update-1.0" in suggestion[0])
if suggestion:
    suggestion[1]()
    pump(1)
check("откат убрал мод из того изменения", not os.path.exists(os.path.join(mods_dir, "broken-update-1.0.jar")))
w._switch_page(mw.PAGE_STATS)
w.sidebar.set_page(mw.PAGE_STATS)
pump(1)
check(f"статистика запусков: {w.stats_page.launch_summary.text()}", "Запусков" in w.stats_page.launch_summary.text())
os.makedirs(f"{SP}/shots/g", exist_ok=True)
w.stats_page.launch_chart.parentWidget().grab().save(f"{SP}/shots/g/launches.png")
w.icon_loader.shutdown()
