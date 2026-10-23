"""Обновление программы: проверка, окно «Доступна версия», загрузка с проверкой SHA-256, замена .exe, перезапуск.
GitHub и скачивание подменены: «новая версия» — локальные байты, «текущий .exe» — файл в рабочей папке."""
import hashlib
import logging
import os
import shutil
import sys
import time

PROJECT, SP = sys.argv[1], sys.argv[2]
GAME = f"{SP}/game2"
SHOTS = f"{SP}/shots/u"
sys.path.insert(0, PROJECT)
logging.basicConfig(level=logging.WARNING)
import config as app_config  # noqa: E402

app_config.CACHE_FILE = f"{SP}/cache/mod_cache.db"
app_config.ICON_CACHE_DIR = f"{SP}/cache/icons96"
app_config.load_config = lambda *a, **k: app_config._normalize({
    "theme": "dark", "current_profile": "Сборка",
    "profiles": {"Сборка": {**app_config.default_profile(), "mods_path": GAME, "manual_mode": True,
                            "game_version": "1.21.11", "loader": "fabric"}},
    "settings": {"auto_check": False, "discover_launchers": False, "app_update_check": False}})
app_config.save_config = lambda *a, **k: None
os.makedirs(SHOTS, exist_ok=True)

import self_update  # noqa: E402

PAYLOAD = b"MZ" + os.urandom(3 * 1024 * 1024)  # «Новый .exe», 3 МБ — чтобы был виден прогресс
CHECKSUM = hashlib.sha256(PAYLOAD).hexdigest()
EXE = os.path.join(SP, "app_update", "ModUpdater.exe")
shutil.rmtree(os.path.dirname(EXE), ignore_errors=True)
os.makedirs(os.path.dirname(EXE))
with open(EXE, "wb") as f:
    f.write(b"old exe")


class FakeSession:
    def get(self, url, stream=False, **kwargs):
        class Response:
            text = f"{CHECKSUM}  ModUpdater.exe\n"
            headers = {"Content-Length": str(len(PAYLOAD))}

            def raise_for_status(self):
                pass

            def iter_content(self, size):
                for i in range(0, len(PAYLOAD), 64 * 1024):
                    time.sleep(0.01)
                    yield PAYLOAD[i:i + 64 * 1024]

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return Response()


RELEASE = self_update.Release(
    version="9.9.9", tag="v9.9.9", page_url="https://example.com/release",
    notes="### Интерфейс\n- Новая кнопка\n- Быстрее список модов\n\n### Исправления\n- Мелкие ошибки",
    exe_url="https://example.com/ModUpdater.exe", checksum_url="https://example.com/ModUpdater.exe.sha256",
    size=len(PAYLOAD), prerelease=False)
self_update.SESSION = FakeSession()
self_update.check = lambda *a, **k: RELEASE
self_update.supported = lambda: True
self_update.current_executable = lambda: EXE
self_update.downloads_folder = lambda: os.path.join(SP, "app_update", "downloads")

from PyQt6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()
import main_window as mw  # noqa: E402

w = mw.ModUpdaterApp()
w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
w.resize(1200, 800)
w.show()
restarts = []
w._restart_app = lambda: restarts.append(True)
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


pump(1)

# Автоматическая проверка: уведомление, строка в настройках
w._check_app_update()
pump(0.5)
check("автопроверка выключена — не проверяем", not getattr(w, "_app_release", None))
w.settings["app_update_check"] = True
w._check_app_update()
pump(1)
check("найдена версия 9.9.9", getattr(w, "_app_release", None) is RELEASE)
check(f"уведомление: {toasts[-1:]}", any("9.9.9" in t for t in toasts))
w._switch_page(mw.PAGE_SETTINGS)
w.sidebar.set_page(mw.PAGE_SETTINGS)
pump(0.8)
page = w.settings_page
check("в настройках: «доступна 9.9.9» и кнопка «Обновить…»",
      "9.9.9" in page.app_update_status.text() and page.app_update_open_btn.isVisible())
page.app_update_status.parentWidget().grab().save(f"{SHOTS}/01_settings_card.png")

# Окно обновления: снимок, затем «Обновить и перезапустить»
state = {"shot": False, "progress_seen": False}


def drive_dialog():
    dialog = QApplication.activeModalWidget()
    if dialog is None:
        QTimer.singleShot(100, drive_dialog)
        return
    if not state["shot"]:
        dialog.grab().save(f"{SHOTS}/02_dialog.png")
        state["shot"] = True
        dialog.install_btn.click()
    if dialog.progress.isVisible() and dialog.progress.value() > 0 and not state["progress_seen"]:
        state["progress_seen"] = True
        dialog.grab().save(f"{SHOTS}/03_downloading.png")
    if dialog.isVisible():
        QTimer.singleShot(50, drive_dialog)


QTimer.singleShot(200, drive_dialog)
w._open_app_update()  # Модальное окно: им управляет drive_dialog
pump(0.5)
check("прогресс загрузки показан", state["progress_seen"])
with open(EXE, "rb") as f:
    check("новый .exe на месте старого", f.read() == PAYLOAD)
check("старый .exe сохранён как *.old", os.path.exists(EXE + ".old"))
check("перезапуск вызван", restarts == [True])

# Пропуск версии: при следующей автопроверке уведомления нет
def skip_dialog():
    dialog = QApplication.activeModalWidget()
    if dialog is None:
        QTimer.singleShot(100, skip_dialog)
        return
    dialog.skip_btn.click()


QTimer.singleShot(200, skip_dialog)
w._open_app_update()
pump(0.3)
check("версия пропущена", w.settings.get("app_update_skip") == "9.9.9")
toasts.clear()
w.settings["app_update_last_check"] = 0
w._check_app_update()
pump(1)
check(f"после пропуска уведомления нет: {toasts}", not any("Доступна новая версия" in t for t in toasts))

# Битая загрузка: контрольная сумма не совпала — файл не трогаем
with open(EXE, "wb") as f:
    f.write(b"old exe")
CHECKSUM = "0" * 64
state.update(shot=False, progress_seen=False)
errors = {}


def failing_dialog():
    dialog = QApplication.activeModalWidget()
    if dialog is None:
        QTimer.singleShot(100, failing_dialog)
        return
    if not state["shot"]:
        state["shot"] = True
        dialog.install_btn.click()
    if dialog.install_btn.text() in ("Повторить", "Retry"):
        errors["text"] = dialog.progress_text.text()
        dialog.grab().save(f"{SHOTS}/04_checksum_error.png")
        dialog.later_btn.click()
        return
    QTimer.singleShot(50, failing_dialog)


QTimer.singleShot(200, failing_dialog)
w._open_app_update()
pump(0.3)
check(f"ошибка контрольной суммы показана: {errors.get('text', '')[:60]}", "сумма" in errors.get("text", ""))
with open(EXE, "rb") as f:
    check("при ошибке .exe не тронут", f.read() == b"old exe")
check("перезапуска не было", restarts == [True])
print("done")
