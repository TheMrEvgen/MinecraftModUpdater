"""Функциональный прогон нового интерфейса: обновление, отмена, история, удаление, .jar, .mrpack, импорт."""
import json
import logging
import os
import shutil
import sys
import time
import zipfile

PROJECT, SP = sys.argv[1], sys.argv[2]
sys.path.insert(0, PROJECT)
logging.basicConfig(level=logging.WARNING)
import config as app_config  # noqa: E402

app_config.CACHE_FILE = f"{SP}/cache/mod_cache.db"
app_config.ICON_CACHE_DIR = f"{SP}/cache/icons96"
game = f"{SP}/game"
mods = f"{game}/mods"
app_config.load_config = lambda *a, **k: app_config._normalize({
    "theme": "dark", "current_profile": "T",
    "profiles": {"T": {**app_config.default_profile(), "mods_path": game, "channel": "release"}},
    "settings": {"auto_check": True, "discover_launchers": False, "changelog_before_update": False}})
app_config.save_config = lambda *a, **k: None

from PyQt6.QtCore import QEventLoop, Qt  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()
import main_window as mw  # noqa: E402

dialogs = []
from window_patch import patch_window  # noqa: E402
patch_window(confirm=lambda *a, **k: (dialogs.append(a[1]), True)[1],
             ask_choice=lambda parent, icon, title, text, buttons, **k: (dialogs.append(title), 1)[1],
             show_message=lambda parent, icon, title, text: dialogs.append(f"MSG {title}: {text[:80]}"))
toasts = []
w = mw.ModUpdaterApp()
orig_show = w.toasts.show
w.toasts.show = lambda text, kind="info", action_text=None, action=None, timeout=5000: (
    toasts.append((text, kind, action_text, action)), orig_show(text, kind, action_text, action, timeout))
w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
w.show()


def pump(sec=0.2):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 30)
        time.sleep(0.01)


def wait_idle(timeout=180):
    end = time.time() + timeout
    pump(0.3)
    while (w._busy is not None) and time.time() < end:
        pump(0.05)
    pump(0.3)
    assert w._busy is None, f"busy={w._busy}"


def statuses():
    return {e["name"]: str(e["status"]) for e in w.model.items()}


def jars():
    return sorted(f for f in os.listdir(mods) if f.endswith(".jar"))


wait_idle()
wait_idle()
print("1. после автопроверки:", statuses())
print("   кнопка:", w.update_all_btn.text(), "| карточки:", {k: c.count for k, c in w.cards.items() if c.isVisible()})

# Обновить все
w.update_all()
wait_idle()
print("2. после обновления:", statuses())
print("   файлы:", jars())
undo = next(t for t in reversed(toasts) if t[2] == "Отменить")
print("   toast:", undo[0])

# История
w._switch_page(mw.PAGE_HISTORY)
pump()
print("3. история: карточек", w.history_page.list_layout.count() - 1)
w._switch_page(mw.PAGE_MODS)

# Отменить через уведомление
undo[3]()
wait_idle()
wait_idle()
print("4. после отмены:", jars())

# Удаление и отмена
sodium = next(e for e in w.model.items() if e["name"] == "Sodium")
w._delete_entries([sodium])
pump()
print("5. после удаления:", jars(), "| в списке:", "Sodium" in statuses())
next(t for t in reversed(toasts) if t[2] == "Отменить")[3]()
wait_idle()
wait_idle()
print("   после отмены удаления:", jars())

# Добавление .jar перетаскиванием
extra_dir = f"{SP}/dropfiles"
os.makedirs(extra_dir, exist_ok=True)
shutil.copy(os.path.join(mods, "unknown-local-mod.jar"), f"{extra_dir}/dropped-mod.jar")
w._add_jars([f"{extra_dir}/dropped-mod.jar"])
wait_idle()
wait_idle()
print("6. после добавления .jar:", jars())
next(t for t in reversed(toasts) if t[2] == "Отменить")[3]()
wait_idle()
wait_idle()

# Экспорт .mrpack и импорт его обратно
__import__('PyQt6.QtWidgets', fromlist=['QFileDialog']).QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (f"{SP}/flow.mrpack", ""))
w.export_mrpack()
index = json.loads(zipfile.ZipFile(f"{SP}/flow.mrpack").read("modrinth.index.json"))
print("7. mrpack:", index["dependencies"], len(index["files"]), "файлов")
w._import_mrpack(f"{SP}/flow.mrpack")
wait_idle()
print("   режим импорта:", w.is_import_mode, "| баннер:", w.banner.isVisible(), "| кнопка:", w.update_all_btn.text())
print("   записи:", statuses())
w._exit_import_mode()
wait_idle()
print("   после отмены импорта:", w.is_import_mode, len(w.model.items()), "модов")

# Строка «Поделиться» -> импорт -> установка (объединение)
from exporters import build_share_data, encode_share_string  # noqa: E402
share = encode_share_string(build_share_data(w.mods_data, w.game_version, w.loader, strict=False))
w._import_text(share)
wait_idle()
print("8. импорт строки:", w.is_import_mode, statuses())
w.update_all()
wait_idle()
wait_idle()
print("   после установки (объединение):", w.is_import_mode, jars())
# Откатываем установку, чтобы песочница осталась в исходном состоянии
from updater import list_transactions, rollback_transaction  # noqa: E402
for m in list_transactions(mods):
    rollback_transaction(mods, m)
print("   итог песочницы:", jars())
print("диалоги:", dialogs)
print("ошибки в уведомлениях:", [t[0] for t in toasts if t[1] == "error"])
w.icon_loader.shutdown()
print("OK")
