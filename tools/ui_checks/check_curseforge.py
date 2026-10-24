"""CurseForge в окне программы: мод, которого нет на Modrinth, опознан, обновлён; каталог ищет на CurseForge.
Нужны ключ API (CURSEFORGE_API_KEY или _build_secrets.py) и интернет; без ключа сценарий пропускается."""
import logging
import os
import shutil
import sys
import time

PROJECT, SP = sys.argv[1], sys.argv[2]
GAME = f"{SP}/game_cf"
SHOTS = f"{SP}/shots/cf"
sys.path.insert(0, PROJECT)
os.chdir(PROJECT)  # _build_secrets.py лежит в папке проекта
logging.basicConfig(level=logging.WARNING)
import curseforge  # noqa: E402

if not curseforge.available():
    print("SKIP нет ключа CurseForge")
    sys.exit(0)

# Песочница: старая бета JourneyMap для 1.21.1 Fabric — её нет на Modrinth, есть на CurseForge
OLD_FILE = (32274, 6662155)
shutil.rmtree(GAME, ignore_errors=True)
os.makedirs(f"{GAME}/mods")
os.makedirs(f"{GAME}/versions/fabric-loader-0.16.0-1.21.1")
with open(f"{GAME}/versions/fabric-loader-0.16.0-1.21.1/fabric-loader-0.16.0-1.21.1.json", "w") as f:
    f.write('{"id": "fabric-loader-0.16.0-1.21.1", "inheritsFrom": "1.21.1"}')
cached = f"{SP}/downloads/curseforge/{OLD_FILE[1]}.jar"
info = curseforge._get(f"/mods/{OLD_FILE[0]}/files/{OLD_FILE[1]}")["data"]
if not os.path.isfile(cached):
    import requests
    os.makedirs(os.path.dirname(cached), exist_ok=True)
    with open(cached, "wb") as f:
        f.write(requests.get(info["downloadUrl"], timeout=120).content)
shutil.copy(cached, f"{GAME}/mods/{info['fileName']}")

import config as app_config  # noqa: E402

app_config.CACHE_FILE = f"{SP}/cache/mod_cache.db"
app_config.ICON_CACHE_DIR = f"{SP}/cache/icons96"
app_config.load_config = lambda *a, **k: app_config._normalize({
    "theme": "dark", "current_profile": "CurseForge",
    "profiles": {"CurseForge": {**app_config.default_profile(), "mods_path": GAME, "channel": "release"}},
    "settings": {"auto_check": True, "discover_launchers": False, "app_update_check": False,
                 "changelog_before_update": False, "auto_snapshot": False}})
app_config.save_config = lambda *a, **k: None
os.makedirs(SHOTS, exist_ok=True)

from PyQt6.QtCore import QEventLoop, Qt  # noqa: E402
from main import create_app  # noqa: E402

app = create_app()
import main_window as mw  # noqa: E402
from window_patch import patch_window  # noqa: E402

patch_window(confirm=lambda *a, **k: True, show_message=lambda *a, **k: None)
w = mw.ModUpdaterApp()
w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
w.resize(1400, 860)
w.show()


def pump(sec=0.3):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 30)
        time.sleep(0.01)


def wait_idle(timeout=120):
    pump(1)
    end = time.time() + timeout
    while time.time() < end and w._busy is not None:
        pump(0.3)
    pump(0.8)


def check(label, condition):
    print(("OK   " if condition else "FAIL ") + label, flush=True)


wait_idle()
entry = next((e for e in w._count_items if e.get("filepath") and "journeymap" in e["filepath"].lower()), None)
check(f"мод опознан на CurseForge: {entry and (entry.get('name'), entry.get('project_id'))}",
      bool(entry) and entry.get("source") == "curseforge" and entry["project_id"] == "cf:32274")
from enums import ModStatus  # noqa: E402
check(f"найдено обновление: {entry and entry.get('current_version')} → {entry and entry.get('new_version')}",
      bool(entry) and entry.get("status") == ModStatus.UPDATE_AVAILABLE and bool(entry.get("download_url")))
w.grab().save(f"{SHOTS}/01_list.png")
w._select_entry(entry)
pump(2)
check(f"панель мода: кнопка «{w.details.page_btn._full_text}»", w.details.page_btn._full_text == "CurseForge")
icon_file = os.path.join(app_config.ICON_CACHE_DIR, "cf_32274.png")
check("логотип мода загружен и сохранён в кэш", os.path.exists(icon_file))
w.details.grab().save(f"{SHOTS}/02_details.png")

from scanner import check_target_version  # noqa: E402
found = check_target_version([entry], "1.21.1", "fabric", w._channel())[0]["version"]
check(f"проверка версии игры (готовность, переход) ищет мод на CurseForge: {found and found['version_number']}",
      bool(found) and found["project_id"] == "cf:32274")

# Обновление
new_name = entry.get("new_filename")
w.start_update([entry])
wait_idle()
files = os.listdir(f"{GAME}/mods")
check(f"обновлено: {sorted(f for f in files if f.endswith('.jar'))}", new_name in files and info["fileName"] not in files)

# Каталог: поиск на CurseForge
w._switch_page(mw.PAGE_CATALOG)
w.sidebar.set_page(mw.PAGE_CATALOG)
pump(1)
page = w.catalog_page
check("переключатель источника виден", page.source_group.buttons()[1].isVisible())
page.source_group.buttons()[1].click()
page.search.setText("waystones")
page.refresh()
end = time.time() + 30
while time.time() < end and (page._loading or not page.hits):
    pump(0.3)
pump(1)
check(f"поиск на CurseForge: {len(page.hits)} — {[h['title'] for h in page.hits[:3]]}",
      bool(page.hits) and all(h["project_id"].startswith("cf:") for h in page.hits))
check(f"категории CurseForge: {page.category_combo.count() - 1}", page.category_combo.count() > 10)
w.grab().save(f"{SHOTS}/03_catalog.png")
page.list.setCurrentRow(0)
page.details.show_hit(page.hits[0])
end = time.time() + 30
while time.time() < end and not page.details.versions:
    pump(0.3)
pump(1)
check(f"панель проекта CurseForge: версий {len(page.details.versions)}", bool(page.details.versions))
w.grab().save(f"{SHOTS}/04_catalog_details.png")
# Импорт сборки CurseForge (.zip с manifest.json)
import requests  # noqa: E402
from view_model import subtitle_for  # noqa: E402

packs = curseforge._get("/mods/search", {"gameId": 432, "classId": 4471, "gameVersion": "1.21.1", "modLoaderType": 4,
                                          "sortField": 6, "sortOrder": "desc", "pageSize": 10})["data"]
pack_file = next(f for p in packs for f in p.get("latestFiles") or []
                 if f.get("downloadUrl") and not f.get("isServerPack") and f.get("fileLength", 0) < 30_000_000)
pack_path = f"{SP}/downloads/curseforge/pack.zip"
with open(pack_path, "wb") as f:
    f.write(requests.get(pack_file["downloadUrl"], timeout=180).content)
w._switch_page(mw.PAGE_MODS)
w.sidebar.set_page(mw.PAGE_MODS)
w._import_mrpack(pack_path)  # .zip — сборка CurseForge
wait_idle()
imported = [e for e in w._count_items if e.get("project_id", "").startswith("cf:")]
check(f"сборка «{pack_file['fileName']}» открыта в режиме импорта: модов {len(imported)}", w.is_import_mode and len(imported) > 10)
manual = [e for e in imported if not e.get("download_url")]
if manual:
    text, _ = subtitle_for(manual[0])
    check(f"мод с запретом скачивания помечен: {manual[0]['name']} — {text}", "CurseForge" in text)
w.grab().save(f"{SHOTS}/05_pack_import.png")
w._exit_import_mode()
print("done")
