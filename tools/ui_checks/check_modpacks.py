"""Готовые сборки из каталога: вкладка «Сборки», окно установки, новая сборка Prism (в песочнице, не в настоящем Prism)."""
import json
import logging
import os
import shutil
import sys
import time

PROJECT, SP = sys.argv[1], sys.argv[2]
FAKE_APPDATA = f"{SP}/fake_appdata"
SHOTS = f"{SP}/shots/mp"
sys.path.insert(0, PROJECT)
logging.basicConfig(level=logging.WARNING)
shutil.rmtree(FAKE_APPDATA, ignore_errors=True)
os.makedirs(f"{FAKE_APPDATA}/PrismLauncher/instances")
import config as app_config  # noqa: E402

app_config.CACHE_FILE = f"{SP}/cache/mod_cache.db"
app_config.ICON_CACHE_DIR = f"{SP}/cache/icons96"
app_config.CONFIG_DIR = f"{SP}/fake_config"
app_config.load_config = lambda *a, **k: app_config._normalize({
    "theme": "dark", "current_profile": "Песочница",
    "profiles": {"Песочница": {**app_config.default_profile(), "mods_path": f"{SP}/game", "manual_mode": True,
                               "game_version": "1.21.1", "loader": "fabric"}},
    "settings": {"auto_check": False, "discover_launchers": False, "app_update_check": False}})
app_config.save_config = lambda *a, **k: None
os.makedirs(SHOTS, exist_ok=True)

import launchers  # noqa: E402

launchers._appdata = lambda: FAKE_APPDATA  # «Prism» — папка в песочнице
import official_launcher  # noqa: E402

FAKE_MC = f"{FAKE_APPDATA}/.minecraft"
os.makedirs(FAKE_MC)
official_launcher.minecraft_dir = lambda: FAKE_MC  # «Minecraft Launcher» — тоже в песочнице
from PyQt6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()
import main_window as mw  # noqa: E402

w = mw.ModUpdaterApp()
w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
w.resize(1300, 840)
w.show()
w._play = lambda: None


def pump(sec=0.3):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 30)
        time.sleep(0.01)


def wait_downloads(label, timeout=300):
    """Сборка качается в фоне: ждём конца загрузки, по пути смотрим индикатор и страницу «Загрузки»."""
    import downloads
    seen = {}
    end = time.time() + timeout
    while time.time() < end:
        pump(0.3)
        active = downloads.MANAGER.active()
        if active and "indicator" not in seen and active[0].totals()["bytes"] > 0:
            pump(1.2)
            seen["indicator"] = w.download_indicator.isVisible()
            w._switch_page(mw.PAGE_DOWNLOADS)
            pump(1.2)
            page = w.downloads_page
            seen["page"] = (page.job_title.text(), page.files_model.rowCount(), page.tiles["speed"].value.text())
            check(f"{label}: на странице идущая загрузка", page.selected is active[0])
            w.grab().save(f"{SHOTS}/{label}_downloads.png")
            w._switch_page(mw.PAGE_MODS)
        if not active and downloads.MANAGER.all():
            break
    pump(1)
    job = downloads.MANAGER.all()[0]
    check(f"{label}: загрузка в фоне, индикатор {seen.get('indicator')}, страница {seen.get('page')}, итог {job.state}",
          job.state == downloads.DONE and (label == "update"  # Один маленький файл — успевает скачаться мгновенно
                                           or seen.get("indicator") and seen.get("page", ("", 0))[1] > 0))


def check(label, condition):
    print(("OK   " if condition else "FAIL ") + label, flush=True)


pump(2)
w._switch_page(mw.PAGE_CATALOG)
w.sidebar.set_page(mw.PAGE_CATALOG)
pump(1)
page = w.catalog_page
tabs = [b.text().strip() for b in page.tab_group.buttons()]
check(f"вкладки каталога: {tabs}", tabs[-1] == "Сборки")
page.tab_group.buttons()[-1].click()
page.compatible.setChecked(False)
page.search.setText("fabulously optimized")
page.refresh()
end = time.time() + 30
while time.time() < end and (page._loading or not page.hits):
    pump(0.3)
pump(1)
check(f"сборки найдены: {[h['title'] for h in page.hits[:3]]}", any("Fabulously" in h["title"] for h in page.hits))
w.grab().save(f"{SHOTS}/01_catalog.png")
hit = next(h for h in page.hits if "Fabulously" in h["title"])
state = {}


def drive():
    dialog = QApplication.activeModalWidget()
    if dialog is None or not hasattr(dialog, "plan"):
        QTimer.singleShot(150, drive)
        return
    if dialog.plan is None:
        QTimer.singleShot(200, drive)
        return
    if "info" not in state:
        state["info"] = dialog.info.text()
        state["prism_offered"] = dialog.prism_radio.isVisible() and dialog.prism_radio.isChecked()
        dialog.grab().save(f"{SHOTS}/02_dialog.png")
        dialog.install_btn.click()
    if dialog.isVisible():
        QTimer.singleShot(200, drive)


QTimer.singleShot(200, drive)
w._install_modpack(hit)
wait_downloads("install")
check(f"окно показало, что внутри: {state.get('info')}", "Minecraft" in state.get("info", ""))
check("предложена новая сборка Prism", state.get("prism_offered"))
instances = os.listdir(f"{FAKE_APPDATA}/PrismLauncher/instances")
check(f"создана сборка Prism: {instances}", len(instances) == 1)
instance = f"{FAKE_APPDATA}/PrismLauncher/instances/{instances[0]}"
pack = json.load(open(f"{instance}/mmc-pack.json", encoding="utf-8"))
uids = [c["uid"] for c in pack["components"]]
check(f"mmc-pack.json: {uids}", "net.minecraft" in uids and "org.lwjgl3" in uids and "net.fabricmc.fabric-loader" in uids)
mods = os.listdir(f"{instance}/minecraft/mods")
check(f"моды скачаны: {len(mods)}, настройки распакованы: {os.path.isdir(instance + '/minecraft/config')}",
      len(mods) > 10 and os.path.isdir(f"{instance}/minecraft/config"))
check(f"сборка в списке слева и открыта: {w.current_profile_name}", w.current_profile_name == instances[0]
      and w.profiles[instances[0]]["source"] == "prism")
w.grab().save(f"{SHOTS}/03_installed.png")

# --- Обновление сборки: делаем вид, что стоит старая версия, и обновляем до текущей ---
import modpack_install  # noqa: E402
from PyQt6.QtWidgets import QLabel, QPushButton  # noqa: E402

game = f"{instance}/minecraft"
saved = modpack_install.load_state(game)
check(f"состав сборки запомнен: {saved and saved['version']}, файлов {len((saved or {}).get('files', []))}",
      bool(saved and saved["project_id"] == hit["project_id"] and saved["files"]))
pack_mod = sorted(f for f in saved["files"] if f.startswith("mods/"))[0]
os.remove(f"{game}/{pack_mod}")                                  # Этот мод «изменился» в новой версии
open(f"{game}/mods/my-own-mod.jar", "wb").write(b"mine")           # Свой мод пользователя
open(f"{game}/mods/old-pack-mod.jar", "wb").write(b"old")          # Был в старой версии, в новой его нет
config_file = sorted(saved["overrides"])[0]
open(f"{game}/{config_file}", "a", encoding="utf-8").write(" # my change")  # Пользователь поменял настройку
saved["version"], saved["files"] = "0.0-old", saved["files"] + ["mods/old-pack-mod.jar"]
json.dump(saved, open(modpack_install.state_path(game), "w", encoding="utf-8"))

w.settings["auto_check"] = True
w._modpack_checked.clear()
w._check_modpack_update()
end = time.time() + 30
texts = []
while time.time() < end and not any("Вышла новая версия" in t for t in texts):
    pump(0.3)
    texts = [" ".join(l.text() for l in t.findChildren(QLabel)) for t in w.toasts.toasts]
check(f"предложено обновить сборку: {[t for t in texts if 'Вышла' in t]}", any("Вышла новая версия" in t for t in texts))
update_state = {}


def drive_update():
    dialog = QApplication.activeModalWidget()
    if dialog is None or not hasattr(dialog, "update_btn"):
        QTimer.singleShot(150, drive_update)
        return
    if dialog.update is None:
        QTimer.singleShot(200, drive_update)
        return
    if "info" not in update_state:
        update_state["info"] = dialog.info.text()
        update_state["plan"] = dialog.update
        update_state["changes"] = dialog.changes.isVisible() and dialog.changes.toPlainText()[:80]
        dialog.grab().save(f"{SHOTS}/04_update_dialog.png")
        dialog.update_btn.click()
    if dialog.isVisible():
        QTimer.singleShot(200, drive_update)
    else:
        update_state["backup"] = dialog.backup


QTimer.singleShot(200, drive_update)
w._update_modpack()
wait_downloads("update")
plan = update_state.get("plan")
check(f"окно обновления: {update_state.get('info')!r}", "0.0-old" in update_state.get("info", ""))
check(f"«Что нового» в окне обновления: {update_state.get('changes')!r}", bool(update_state.get("changes")))
check(f"план: скачать {plan and [f.path for f in plan.download]}, убрать {plan and plan.remove}, "
      f"свои моды {plan and plan.user_mods}, свои настройки {plan and plan.kept}",
      plan and [f.path for f in plan.download] == [pack_mod] and plan.remove == ["mods/old-pack-mod.jar"]
      and plan.user_mods == ["my-own-mod.jar"] and plan.kept == [config_file])
check("мод сборки снова на месте, свой мод остался, старый убран",
      os.path.isfile(f"{game}/{pack_mod}") and os.path.isfile(f"{game}/mods/my-own-mod.jar")
      and not os.path.exists(f"{game}/mods/old-pack-mod.jar"))
check("изменённая настройка не перезаписана", "# my change" in open(f"{game}/{config_file}", encoding="utf-8").read())
check(f"версия сборки обновлена: {modpack_install.load_state(game)['version']}",
      modpack_install.load_state(game)["version"] != "0.0-old")
w.grab().save(f"{SHOTS}/05_updated.png")
undo = [b for t in w.toasts.toasts for b in t.findChildren(QPushButton) if b.objectName() == "ToastAction"
        and b.text() == "Отменить"]
check(f"есть кнопка «Отменить»: {len(undo)}", len(undo) == 1)
if undo:
    undo[0].click()
    pump(1)
    check("отмена вернула прежнее состояние",
          not os.path.exists(f"{game}/{pack_mod}") and os.path.isfile(f"{game}/mods/old-pack-mod.jar")
          and modpack_install.load_state(game)["version"] == "0.0-old")

# --- Установка в Minecraft Launcher ---
official_state = {}


def drive_official():
    dialog = QApplication.activeModalWidget()
    if dialog is None or not hasattr(dialog, "official_radio"):
        QTimer.singleShot(150, drive_official)
        return
    if dialog.plan is None:
        QTimer.singleShot(200, drive_official)
        return
    if "offered" not in official_state:
        official_state["offered"] = dialog.official_radio.isVisible()
        dialog.official_radio.setChecked(True)
        dialog.name_edit.setText("FO Official")
        dialog.grab().save(f"{SHOTS}/06_official_dialog.png")
        dialog.install_btn.click()
    if dialog.isVisible():
        QTimer.singleShot(200, drive_official)


QTimer.singleShot(200, drive_official)
w._install_modpack(hit)
wait_downloads("install")
check("предложена установка в Minecraft Launcher", official_state.get("offered"))
installs = official_launcher.installations(FAKE_MC)
check(f"установка в launcher_profiles.json: {[(i['name'], i['version_id']) for i in installs]}",
      len(installs) == 1 and installs[0]["name"] == "FO Official" and installs[0]["version_id"].startswith("fabric-loader-"))
vid = installs[0]["version_id"] if installs else ""
check(f"версия Fabric в versions: {vid}", os.path.isfile(f"{FAKE_MC}/versions/{vid}/{vid}.json"))
check(f"моды в своей папке игры: {installs and installs[0]['game_dir']}",
      bool(installs) and len(os.listdir(f"{installs[0]['game_dir']}/mods")) > 10)
profile = w._profile()
check(f"сборка открыта и связана с установкой: {w.current_profile_name}",
      w.current_profile_name == "FO Official" and profile.get("source") == "official"
      and profile.get("launcher_id") == (installs and installs[0]["id"]))
target = w._launcher_target()
check(f"версии меняет программа: {target}", target is not None and target.kind == "official")
found = [i.name for i in launchers.find_official_instances(FAKE_MC)]
check(f"поиск сборок находит установку: {found}", found == ["FO Official"])

# --- Очередь сборок, пауза и кнопки страницы «Загрузки» (подставные потоки — без настоящих загрузок) ---
import downloads  # noqa: E402
from PyQt6.QtCore import pyqtSignal  # noqa: E402
from workers import SafeThread  # noqa: E402


class FakeWorker(SafeThread):
    installed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, seconds):
        super().__init__()
        self.seconds, self.stopped = seconds, False

    def stop(self):
        self.stopped = True

    def run(self):
        end = time.time() + self.seconds
        while time.time() < end and not self.stopped:
            time.sleep(0.05)


downloads.MANAGER.clear_finished()
fakes = {}
for name, seconds in (("A", 2.0), ("B", 0.5), ("C", 0.5)):
    job = w._start_download(name, "modpack")
    job.add_file("f", "file", 100)
    worker = FakeWorker(seconds)
    worker.finished.connect(lambda j=job: j.finish(downloads.DONE))
    fakes[name] = (job, worker)
    w._queue_modpack(job, worker)
a, b, c = (fakes[n][0] for n in "ABC")
check(f"очередь: A качается, B и C ждут ({a.state}, {b.state}, {c.state})",
      a.state == downloads.ACTIVE and b.state == c.state == downloads.WAITING)
w._switch_page(mw.PAGE_DOWNLOADS)
page = w.downloads_page
page.select(c)
page._picked = True
pump(0.5)
check("у ждущей загрузки кнопки «Раньше»/«Позже», без паузы",
      page.up_btn.isVisible() and page.down_btn.isVisible() and not page.pause_btn.isVisible())
page.up_btn.click()  # C — раньше B
pump(0.3)
page.select(a)
pump(0.3)
page.pause_btn.click()
pump(0.3)
check(f"пауза: {a.paused}, кнопка «{page.pause_btn.text()}»", a.paused and page.pause_btn.text() == "Продолжить")
page.pause_btn.click()
w.grab().save(f"{SHOTS}/07_queue.png")
end = time.time() + 10
while time.time() < end and c.state != downloads.ACTIVE and c.finished is None:
    pump(0.1)
check(f"после A началась C (её подняли), B ждёт: C {c.state}, B {b.state}",
      (c.state == downloads.ACTIVE or c.finished) and b.state == downloads.WAITING)
b.cancel()
pump(0.3)
check(f"отмена ждущей загрузки: {b.state}", b.state == downloads.CANCELLED and fakes["B"][1] not in
      [item[1] for item in w._modpack_queue])
end = time.time() + 10
while time.time() < end and downloads.MANAGER.active():
    pump(0.1)
check("очередь опустела", not downloads.MANAGER.active() and not w._modpack_queue)
print("done")
