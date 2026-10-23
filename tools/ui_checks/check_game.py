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
import main_window_tools as mwt  # noqa: E402

opened = []
for module in (mw, mwt):
    module.confirm = lambda *a, **k: True
    module.show_message = lambda *a, **k: None
mwt.ask_choice = lambda *a, **k: 0  # «Автоматически»
mwt.CrashDialog.exec = lambda self: opened.append(self.file_combo.currentText()) or 0

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
w.icon_loader.shutdown()
