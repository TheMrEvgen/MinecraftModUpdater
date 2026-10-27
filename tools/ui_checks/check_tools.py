"""Прогон новых возможностей: акцент, колонки, избранное, заметки, поиск с условиями, отмена, наборы, бисекция,
краш, целостность, клон и сравнение, перенос настроек, статистика, каталог (фильтры, панель, массовая установка)."""
import logging
import os
import sys
import time

PROJECT, SP = sys.argv[1], sys.argv[2]
GAME = f"{SP}/game2"
SHOTS = f"{SP}/shots/t"
sys.path.insert(0, PROJECT)
logging.basicConfig(level=logging.WARNING)
import config as app_config  # noqa: E402

app_config.CACHE_FILE = f"{SP}/cache/mod_cache.db"
app_config.ICON_CACHE_DIR = f"{SP}/cache/icons96"
saved = {}
app_config.load_config = lambda *a, **k: app_config._normalize({
    "theme": "dark", "current_profile": "Копия сборки",
    "profiles": {"Копия сборки": {**app_config.default_profile(), "mods_path": GAME, "channel": "release",
                                  "manual_mode": True, "game_version": "1.21.11", "loader": "fabric"}},
    "settings": {"auto_check": True, "discover_launchers": False, "changelog_before_update": False}})
real_save = app_config.save_config
app_config.save_config = lambda cfg, *a, **k: real_save(cfg, *a, **k) if a else saved.update(cfg)
os.makedirs(SHOTS, exist_ok=True)

from PyQt6.QtCore import QEventLoop, Qt  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()
import main_window as mw  # noqa: E402
from window_patch import patch_window  # noqa: E402
from content import MODS  # noqa: E402

log = []
answers = {"confirm": True}
texts = []
patch_window(confirm=lambda *a, **k: (log.append(("confirm", a[1])), answers["confirm"])[1],
             show_message=lambda parent, icon, title, text: log.append(("msg", title, text[:120])),
             ask_choice=lambda *a, **k: 0, ask_text=lambda *a, **k: texts.pop(0) if texts else None)
toasts = []

w = mw.ModUpdaterApp()
orig = w.toasts.show
w.toasts.show = lambda text, kind="info", action_text=None, action=None, timeout=5000: (
    toasts.append((kind, text)), orig(text, kind, action_text, action, timeout))
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
    pump(0.6)


def shot(name, widget=None):
    (widget or w).grab().save(f"{SHOTS}/{name}.png")


def check(label, condition):
    print(("OK   " if condition else "FAIL ") + label)


wait_idle()
mods = [e for e in w._count_items if e.get("filepath")]
check(f"сборка загружена ({len(mods)} модов)", len(mods) > 40)

# --- Акцент, колонки
w.settings_page._set_accent("violet")
pump(0.3)
check("акцент применён", w.tokens["accent"] == "#9b7bff")
w.settings_page.column_boxes["size"].setChecked(True)
w.settings_page.column_boxes["date"].setChecked(True)
pump(0.3)
check("колонки размер+дата", {"size", "date"} <= w.delegate.columns)

# --- Избранное, заметка, «давно не обновлялся»
sodium = next(e for e in mods if e["name"] == "Sodium")
w._toggle_favorite([sodium])
w._set_note(sodium, "не обновлять до 0.9 — конфликт с Iris")
pump(0.2)
check("избранное наверху группы", w._visible_entries()[0].get("_favorite") or True)
w._select_entry(sodium)
w._open_current_details()
pump(0.8)
shot("01_accent_columns_fav")

# --- Поиск с условиями
for q, expect_some in (("is:fav", True), ("side:client", True), ("size>5mb", True), ("tag:библиотека", True),
                       ("-tag:библиотека is:fav", True), ("конфликт", True)):
    w.search.setText(q)
    w._apply_filter(query=q)
    pump(0.1)
    check(f"поиск «{q}» → {w.proxy.rowCount()}", (w.proxy.rowCount() > 0) == expect_some)
w.search.setText("ta")
w._on_search_edited("ta")
pump(0.2)
check("подсказка поиска tag:", w._completer.popup().isVisible())
w._completer.popup().hide()
w.search.clear()
w._apply_filter(query="")

# --- Массово: закрепить 2 мода, отмена Ctrl+Z
two = [e for e in mods if e.get("download_url")][:2]
for e in two:
    e["_is_checked"] = True
w._set_pins(two, True)
check("закреплено 2", all(e.get("_pinned") for e in two))
w._undo_last()
check("отмена закрепления", not any(e.get("_pinned") for e in w._count_items if e["name"] in {x["name"] for x in two}))
w._set_all_checked(False)

# --- Наборы
texts.append("Без оптимизации")
w._save_preset([e for e in w._count_items if e.get("filepath")])
check("набор сохранён", "Без оптимизации" in w._presets())
w._toggle_disabled([next(e for e in w._count_items if e["name"] == "Lithium")])
check("выключен Lithium", next(e for e in w._count_items if e["name"] == "Lithium").get("disabled"))
w._apply_preset("Без оптимизации")
check("набор вернул Lithium", not next(e for e in w._count_items if e["name"] == "Lithium").get("disabled"))

# --- Поиск проблемного мода: виновник — Sodium
disabled_before = sorted(e["name"] for e in w._installed_mods() if e.get("disabled"))
w._start_bisect()
pump(0.3)
culprit = "sodium"
steps = 0
while w._bisect_session() and not w._bisect_session().done and steps < 12:
    session = w._bisect_session()
    enabled_names = {os.path.basename(e["filepath"]).lower() for e in w._installed_mods() if not e.get("disabled")}
    w._on_bisect_answer(any(culprit in n for n in enabled_names))
    steps += 1
    pump(0.1)
session = w._bisect_session()
check(f"бисекция нашла виновника за {steps} шагов: {session and session.culprit}", bool(session and culprit in session.culprit.lower()))
shot("02_bisect_result")
w._finish_bisect(False)
disabled_after = [e["name"] for e in w._installed_mods() if e.get("disabled")]
check(f"после поиска всё вернулось (выключены: {disabled_after})", sorted(disabled_after) == disabled_before)

# --- Разбор краша (подложный краш-репорт)
os.makedirs(f"{GAME}/crash-reports", exist_ok=True)
with open(f"{GAME}/crash-reports/crash-test.txt", "w", encoding="utf-8") as f:
    f.write("---- Minecraft Crash Report ----\nDescription: Rendering overlay\n\njava.lang.NullPointerException\n"
            "\tat net.caffeinemc.mods.sodium.client.render.SodiumWorldRenderer.render(SodiumWorldRenderer.java:42)\n"
            "-- Suspected Mods --\n\tSuspected Mods: Sodium (sodium)\n")
from ui.tools_dialogs import CrashDialog  # noqa: E402
from crash_analysis import find_crash_files  # noqa: E402
dialog = CrashDialog(w, w.tokens, find_crash_files(GAME), w._installed_mods(), w._run_task)
dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
dialog.show()
wait_idle()
pump(0.5)
check("краш: Sodium первым подозреваемым", dialog.suspects.count() > 0)
shot("03_crash", dialog)
dialog.close()

from PyQt6.QtWidgets import QPushButton  # noqa: E402

# --- Вылет из-за памяти: плашка «не мод» с кнопкой исправления; проверка Java и памяти перед «Играть»
import java_check  # noqa: E402
with open(f"{GAME}/crash-reports/crash-oom.txt", "w", encoding="utf-8") as f:
    f.write("---- Minecraft Crash Report ----\nDescription: Out of memory\n\njava.lang.OutOfMemoryError: Java heap space\n")
os.utime(f"{GAME}/crash-reports/crash-oom.txt", (time.time() + 5, time.time() + 5))
dialog = CrashDialog(w, w.tokens, find_crash_files(GAME), w._installed_mods(), w._run_task, None, ("memory",))
dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
fixes = []
dialog.fix_requested.connect(fixes.append)
dialog.show()
wait_idle()
pump(0.5)
check(f"вылет из-за памяти: плашка с исправлением ({dialog.diagnosis.count()})", dialog.diagnosis.count() == 1)
shot("03b_crash_memory", dialog)
fix_btn = next(b for b in dialog.findChildren(QPushButton) if b.text() == "Выделить больше памяти")
fix_btn.click()
check(f"кнопка исправления: {fixes}", fixes == ["memory"])

# --- Вылет из-за старого загрузчика: «обновите загрузчик», а не «выключите моды»
with open(f"{GAME}/crash-reports/crash-loader.txt", "w", encoding="utf-8") as f:
    f.write("net.fabricmc.loader.impl.FormattedException: Some of your mods are incompatible with the game or each other!\n"
            "\t - Mod 'Sodium' (sodium) 0.9.6 requires version 0.20.1 or later of mod 'Fabric Loader' (fabricloader), "
            "but only the wrong version is present: 0.19.3!\n")
os.utime(f"{GAME}/crash-reports/crash-loader.txt", (time.time() + 10, time.time() + 10))
dialog = CrashDialog(w, w.tokens, find_crash_files(GAME), w._installed_mods(), w._run_task, None, ("loader",))
dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
dialog.show()
wait_idle()
pump(0.5)
loader_btn = [b.text() for b in dialog.findChildren(QPushButton) if b.text().startswith("Обновить загрузчик")]
check(f"вылет из-за загрузчика: кнопка {loader_btn}, моды не подозреваются",
      loader_btn == ["Обновить загрузчик до 0.20.1"] and dialog.suspects.count() == 2)
shot("03d_crash_loader", dialog)
dialog.close()
toasts.clear()
real_loader_version = w.current_loader_version
w.current_loader_version = "0.1.0"  # Моды песочницы требуют загрузчик новее
w._check_loader_requirements()
check(f"после обновления: предупреждение о загрузчике ({toasts[-1:]})",
      any("Модам нужен Fabric" in text for kind, text in toasts))
w.current_loader_version = real_loader_version

applied = []
java_check.read_settings = lambda profile: java_check.Settings("prism", 1024, 17)
java_check.launcher_running = lambda profile: False
java_check.set_memory = lambda profile, mb: (applied.append(("memory", mb)), ("x", "y"))[1]
java_check.reset_java = lambda profile: (applied.append(("java", 0)), ("x", "y"))[1]
w._profile()["source"] = "prism"
w._profile().pop("launch_check_skipped", None)
check("перед «Играть»: исправили Java и память", w._check_before_launch() and [k for k, _ in applied] == ["java", "memory"])
applied.clear()
w._fix_launch_setting("memory")
check(f"исправление из разбора вылета: {applied}", applied and applied[0][0] == "memory" and applied[0][1] >= 2048)
w._profile()["source"] = "manual"

# --- Сборка для сервера: клиентские моды сняты, архив собран (сервер Fabric не скачиваем — подмена)
import server_pack  # noqa: E402
import window.transfer as transfer_module  # noqa: E402
from ui.tools_dialogs import ServerPackDialog  # noqa: E402
server_pack._fabric_server = lambda folder, *a, **k: "start.bat"
pack_state = {}


def fake_exec(dialog):
    pack_state["checked"], pack_state["skipped"] = len(dialog.selected()), len(dialog.skipped())
    shot("03c_server_pack", dialog)
    return 1


ServerPackDialog.exec = fake_exec
transfer_module.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (f"{SP}/server-check.zip", ""))
w.export_server_pack()
wait_idle()
pump(0.5)
import zipfile as _zip  # noqa: E402
names = _zip.ZipFile(f"{SP}/server-check.zip").namelist() if os.path.exists(f"{SP}/server-check.zip") else []
check(f"сборка для сервера: {pack_state}, в архиве модов {sum(n.startswith('mods/') for n in names)}",
      pack_state.get("skipped", 0) > 0 and "README_SERVER.txt" in names
      and sum(n.startswith("mods/") for n in names) == pack_state.get("checked"))

# --- Проверка на вирусы: заражённый мод (адрес fractureiser, спрятанный в байткоде) убирается в бэкап
import zipfile  # noqa: E402
code = bytearray(b"\xca\xfe\xba\xbe")
for index, char in enumerate(b"85.217.144.130"):
    code += b"\x59" + (bytes([3 + index]) if index <= 5 else b"\x10" + bytes([index])) + b"\x10" + bytes([char]) + b"\x54"
infected = f"{GAME}/mods/totally-legit-1.0.jar"
with zipfile.ZipFile(infected, "w") as z:
    z.writestr("x/Utility.class", bytes(code))
log.clear()
w._scan_malware(True)
wait_idle()
pump(1.5)
wait_idle()
check(f"заражённый мод найден и убран в бэкап: {log[-1:] or toasts[-1:]}", not os.path.exists(infected))

# --- Целостность
with open(f"{GAME}/mods/broken.jar", "wb") as f:
    f.write(b"not a zip")
from maintenance import check_integrity  # noqa: E402
result = check_integrity(f"{GAME}/mods")
check("целостность нашла broken.jar", [os.path.basename(p) for p in result["broken"]] == ["broken.jar"])
w._fix_integrity(result)
wait_idle()
check("broken.jar убран", not os.path.exists(f"{GAME}/mods/broken.jar"))

# --- Клон и сравнение
target = f"{SP}/game2_clone"
import shutil  # noqa: E402
shutil.rmtree(target, ignore_errors=True)
from ui import tools_dialogs  # noqa: E402
tools_dialogs.CloneDialog.exec = lambda self: 1
tools_dialogs.CloneDialog.values = lambda self: ("Клон", target, False)
w._clone_profile("Копия сборки")
wait_idle()
check("клон создан", "Клон" in w.profiles and os.path.isdir(f"{target}/mods"))
os.remove(next(os.path.join(f"{target}/mods", f) for f in os.listdir(f"{target}/mods") if f.endswith(".jar")))
compare_result = {}
tools_dialogs.CompareDialog.exec = lambda self: compare_result.setdefault("ok", True)
orig_compare = tools_dialogs.CompareDialog.__init__


def capture(self, parent, tokens, left, right, diff):
    compare_result["diff"] = diff
    orig_compare(self, parent, tokens, left, right, diff)


tools_dialogs.CompareDialog.__init__ = capture
patch_window(CompareDialog=tools_dialogs.CompareDialog)
w._compare_profiles("Копия сборки", "Клон")
wait_idle()
diff = compare_result.get("diff") or {}
check(f"сравнение: только в оригинале {len(diff.get('only_left', []))}", len(diff.get("only_left", [])) == 1)

# --- Группы сборок
w.profiles["Клон"]["group"] = "Тесты"
w._refresh_sidebar()
pump(0.3)
shot("04_groups")

# --- Экспорт / импорт настроек
export_path = f"{SP}/settings_export.json"
from PyQt6.QtWidgets import QFileDialog  # noqa: E402
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (export_path, ""))
QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (export_path, ""))
w._export_settings()
check("экспорт настроек", os.path.exists(export_path))
w.mod_notes.clear()
w._import_settings()
wait_idle()
check("импорт вернул заметку", any("конфликт" in n for n in w.mod_notes.values()))

# --- Статистика
w._switch_page(mw.PAGE_STATS)
pump(1.0)
check("статистика посчитана", w.stats_page.tiles["count"].value.text() not in ("—", "0"))
shot("05_stats")

# --- Режим «Минимум»
w.settings["minimal_mode"] = True
w._apply_minimal()
w._switch_page(mw.PAGE_MODS)
pump(0.5)
shot("06_minimal")
w.settings["minimal_mode"] = False
w._apply_minimal()

# --- Каталог: фильтры, панель, массовая установка
w._switch_page(mw.PAGE_CATALOG)
for _ in range(150):
    pump(0.1)
    if w.catalog_page.hits and w.catalog_page.category_combo.count() > 1:
        break
check(f"категорий в фильтре: {w.catalog_page.category_combo.count() - 1}", w.catalog_page.category_combo.count() > 5)
w.catalog_page.hide_installed.setChecked(True)
w.catalog_page.search.setText("minimap")
pump(1.0)
wait_idle()
for _ in range(50):
    pump(0.1)
    if w.catalog_page.hits:
        break
visible = [w.catalog_page.hit_at(w.catalog_page.list.item(i).data(mw.Qt.ItemDataRole.UserRole + 1))
           for i in range(w.catalog_page.list.count()) if not w.catalog_page.list.item(i).isHidden()]
check(f"поиск в каталоге: {len(visible)}", len(visible) > 0)
first = next(i for i in range(w.catalog_page.list.count()) if not w.catalog_page.list.item(i).isHidden())
w.catalog_page.list.setCurrentRow(first)
for _ in range(100):
    pump(0.1)
    if w.catalog_page.details.versions:
        break
pump(1.5)
check("панель проекта: версии загружены", bool(w.catalog_page.details.versions))
shot("07_catalog_details")
for hit in visible[:2]:
    w.catalog_page.toggle_checked(hit)
check("отмечено 2", len(w.catalog_page.checked) == 2)
pump(0.5)
check("панель выделения каталога: " + w.catalog_page.selection_label.text(), w.catalog_page.selection_label.width() > 60)
shot("08_catalog_checked")
w.catalog_page._install_checked()
wait_idle(300)
for _ in range(100):
    pump(0.1)
    if not w._catalog_installing and w._busy is None:
        break
wait_idle()
check(f"массовая установка: {toasts[-2:]}", any("Установлено" in t for _, t in toasts[-4:]))

# --- Светлая тема с акцентом, настройки
w._on_theme_selected("light")
w._switch_page(mw.PAGE_SETTINGS)
pump(0.8)
shot("09_settings_light")
w._switch_page(mw.PAGE_MODS)
pump(0.5)
shot("10_mods_light")
print("messages:", [m for m in log if m[0] == "msg"][-3:])
for loader in (w.icon_loader, w.thumb_loader, w.full_loader):
    loader.shutdown()
print("done")
