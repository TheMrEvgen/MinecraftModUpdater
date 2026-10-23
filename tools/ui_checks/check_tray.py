"""Работа в фоне: фоновая проверка во всех сборках, уведомление только о новом, закрытие в трей, выход из трея.
Уведомления Windows подменены записью — на рабочем столе ничего не всплывает."""
import logging
import os
import sys
import time

PROJECT, SP = sys.argv[1], sys.argv[2]
GAME = f"{SP}/game"
SHOTS = f"{SP}/shots/t"
sys.path.insert(0, PROJECT)
logging.basicConfig(level=logging.WARNING)
import config as app_config  # noqa: E402

app_config.CACHE_FILE = f"{SP}/cache/mod_cache.db"
app_config.ICON_CACHE_DIR = f"{SP}/cache/icons96"
app_config.load_config = lambda *a, **k: app_config._normalize({
    "theme": "dark", "current_profile": "Песочница",
    "profiles": {"Песочница": {**app_config.default_profile(), "mods_path": GAME, "channel": "release",
                               "manual_mode": True, "game_version": "1.21.1", "loader": "fabric"}},
    "settings": {"auto_check": False, "discover_launchers": False, "app_update_check": False,
                 "close_to_tray": True, "background_check": True}})
app_config.save_config = lambda *a, **k: None
os.makedirs(SHOTS, exist_ok=True)

from PyQt6.QtCore import QEventLoop, Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()
app.setQuitOnLastWindowClosed(False)  # Как в run_gui
quits = []
app.quit = lambda: quits.append(True)
import main_window as mw  # noqa: E402

w = mw.ModUpdaterApp()
w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
w.resize(1200, 800)
w.show()
messages = []
if w.tray:
    w.tray.showMessage = lambda title, text, *a, **k: messages.append((title, text))
toasts = []
original_show = w.toasts.show
w.toasts.show = lambda text, *a, **k: (toasts.append(text), original_show(text, *a, **k))


def pump(sec=0.3):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 30)
        time.sleep(0.01)


def check(label, condition):
    print(("OK   " if condition else "FAIL ") + label, flush=True)


def wait_background(timeout=120):
    end = time.time() + timeout
    while time.time() < end and w._bg_worker and w._bg_worker.isRunning():
        pump(0.3)
    pump(0.5)


pump(1)
check("значок в трее создан и виден (включено «оставаться в трее»)", bool(w.tray) and w.tray.isVisible())
check("первая проверка нужна", w._background_due())

# Окно не в фокусе (WA_DontShowOnScreen) — сообщение идёт уведомлением Windows
w._start_background_check()
wait_background()
total = sum(len(r.updates) for r in w._bg_results)
check(f"фоновая проверка нашла обновления: {total}", total > 0)
check(f"уведомление: {messages[-1:]}", bool(messages) and "Песочница" in messages[-1][1])
check("подсказка значка с числом обновлений", str(total) in w.tray.toolTip())
check("о показанных обновлениях запомнено", bool(w.settings.get("notified_updates", {}).get("Песочница")))
check("следующая проверка — не раньше чем через интервал", not w._background_due())

messages.clear()
w._start_background_check()
wait_background()
check(f"повторно о тех же обновлениях не напоминаем: {messages}", not messages)

# Ручная проверка из меню значка — сообщает даже о старом
w._start_background_check(manual=True)
wait_background()
check("ручная проверка сообщает о найденном", bool(messages) or any("обновления" in t.lower() for t in toasts))

# Настройки: карточка «Работа в фоне»
w._switch_page(mw.PAGE_SETTINGS)
w.sidebar.set_page(mw.PAGE_SETTINGS)
pump(0.8)
page = w.settings_page
check("в настройках видно время последней проверки", "Последняя проверка" in page.background_status.text())
page.close_to_tray.parentWidget().grab().save(f"{SHOTS}/01_background_card.png")

# Закрытие окна — в трей
messages.clear()
w.close()
pump(0.5)
check("окно спрятано, программа работает", not w.isVisible() and not quits)
check(f"подсказка о трее показана один раз: {messages}", len(messages) == 1)
w.show()
w.close()
pump(0.3)
check("второй раз подсказки нет", len(messages) == 1)

# Щелчок по уведомлению — окно открывается на странице модов
w._on_tray_message_clicked()
pump(0.5)
check("окно открыто по уведомлению", w.isVisible() and w.stack.currentIndex() == mw.PAGE_MODS)

# Выход из меню значка — по-настоящему
w._quit_from_tray()
pump(0.5)
check("выход из трея завершает программу", bool(quits) and not w.tray.isVisible())
print("done")
