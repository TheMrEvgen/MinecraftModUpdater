"""
Проверка интерфейса «вживую»: сценарии запускают настоящее окно программы (без показа на экране)
на подготовленных папках-песочницах и проходят по основным функциям.

    python tools/ui_checks/run.py                  — все сценарии
    python tools/ui_checks/run.py tools game       — только выбранные
    python tools/ui_checks/run.py --work D:\\tmp\\ui  — своя рабочая папка (по умолчанию во временной)

Нужен интернет: моды для песочниц скачиваются с Modrinth (один раз, потом берутся из папки downloads),
а сценарии проверяют обновления и каталог. Перед каждым сценарием песочницы создаются заново.
Снимки экрана сохраняются в <рабочая папка>/shots*, полный вывод — в <рабочая папка>/logs.

Песочницы:
    game   — Fabric 1.21.1: три старые версии модов (есть обновления) и один «неизвестный» мод
    game2  — Fabric 1.21.11: 51 мод точных версий из pack_large.json (Sodium, Lithium, Jade и др.)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Dict, List, Optional

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(os.path.dirname(HERE))
SCENARIOS = ["flow", "features", "tools", "audit", "game", "app_update", "tray", "curseforge", "datapacks"]
SMALL_PACK = ("sodium", "lithium", "modmenu")  # Самые старые версии под 1.21.1 — для них точно есть обновления
SMALL_GAME_VERSION = "1.21.1"
LOADER_VERSION_ID = "fabric-loader-0.16.0-1.21.1"
LEFTOVERS = ("game2_clone", "dropfiles", "flow.mrpack", "settings_export.json")
TIMEOUT = 300

session = requests.Session()
session.headers["User-Agent"] = "TheMrEvgen/MinecraftModUpdater-ui-checks"


def download(url: str, target: str) -> str:
    if not os.path.isfile(target):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        response = session.get(url, timeout=120)
        response.raise_for_status()
        with open(target + ".part", "wb") as f:
            f.write(response.content)
        os.replace(target + ".part", target)
    return target


def small_pack_files(downloads: str) -> List[str]:
    """Самая старая версия каждого мода под 1.21.1 (список запоминается, чтобы не зависеть от новых выпусков)."""
    manifest = os.path.join(downloads, "small", "manifest.json")
    if not os.path.isfile(manifest):
        files = []
        for slug in SMALL_PACK:
            versions = session.get(f"https://api.modrinth.com/v2/project/{slug}/version", timeout=30, params={
                "loaders": '["fabric"]', "game_versions": json.dumps([SMALL_GAME_VERSION])}).json()
            primary = next(f for f in versions[-1]["files"] if f["primary"])
            files.append({"filename": primary["filename"], "url": primary["url"]})
        os.makedirs(os.path.dirname(manifest), exist_ok=True)
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump(files, f, indent=1)
    with open(manifest, encoding="utf-8") as f:
        files = json.load(f)
    return [download(item["url"], os.path.join(downloads, "small", item["filename"])) for item in files]


def large_pack_files(downloads: str) -> List[str]:
    with open(os.path.join(HERE, "pack_large.json"), encoding="utf-8") as f:
        pack = json.load(f)
    return [download(item["url"], os.path.join(downloads, "large", item["filename"])) for item in pack]


def _fresh_dir(path: str):
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path)


def prepare(work: str, small: List[str], large: List[str]):
    """Чистые песочницы и имитация игры; кэш программы (work/cache) сохраняется между сценариями."""
    game = os.path.join(work, "game")
    _fresh_dir(game)
    os.makedirs(os.path.join(game, "mods"))
    for path in small:
        shutil.copy(path, os.path.join(game, "mods"))
    with open(os.path.join(game, "mods", "unknown-local-mod.jar"), "wb") as f:
        f.write(b"PK\x05\x06" + b"\0" * 18)  # Пустой zip: мод, которого нет на Modrinth
    version_dir = os.path.join(game, "versions", LOADER_VERSION_ID)
    os.makedirs(version_dir)
    with open(os.path.join(version_dir, LOADER_VERSION_ID + ".json"), "w", encoding="utf-8") as f:
        json.dump({"id": LOADER_VERSION_ID, "inheritsFrom": SMALL_GAME_VERSION}, f)

    game2 = os.path.join(work, "game2")
    _fresh_dir(game2)
    for folder in ("mods", "shaderpacks", "resourcepacks"):
        os.makedirs(os.path.join(game2, folder))
    for path in large:
        shutil.copy(path, os.path.join(game2, "mods"))

    os.makedirs(os.path.join(work, "fakegame"), exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_game.py"), os.path.join(work, "fakegame", "fake_game.py"))
    for name in LEFTOVERS:
        path = os.path.join(work, name)
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.isfile(path):
            os.remove(path)


def run_scenario(name: str, work: str) -> Dict:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "MODUPDATER_LANG": "ru"}
    log_path = os.path.join(work, "logs", f"{name}.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    started = time.monotonic()
    try:
        result = subprocess.run([sys.executable, os.path.join(HERE, f"check_{name}.py"), PROJECT, work],
                                env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=TIMEOUT)
        output, code = result.stdout + result.stderr, result.returncode
    except subprocess.TimeoutExpired as e:
        output, code = f"{e.stdout or ''}{e.stderr or ''}\nПРЕВЫШЕНО ВРЕМЯ ({TIMEOUT} с)", -1
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(output)
    problems = [line for line in output.splitlines() if line.startswith("FAIL ") or line.startswith("Traceback")]
    skipped = next((line for line in output.splitlines() if line.startswith("SKIP ")), None)
    if code != 0 and not problems:
        problems = output.strip().splitlines()[-3:]
    return {"name": name, "ok": code == 0 and not problems, "seconds": time.monotonic() - started,
            "problems": problems, "log": log_path, "skipped": skipped}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка интерфейса Minecraft Mod Updater на песочницах")
    parser.add_argument("scenarios", nargs="*", help=f"какие сценарии запустить: {', '.join(SCENARIOS)}")
    parser.add_argument("--work", default=os.path.join(tempfile.gettempdir(), "modupdater-ui-checks"))
    args = parser.parse_args(argv)
    unknown = [name for name in args.scenarios if name not in SCENARIOS]
    if unknown:
        parser.error(f"неизвестные сценарии: {', '.join(unknown)}")
    work = os.path.abspath(args.work)
    names = args.scenarios or SCENARIOS
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"Рабочая папка: {work}")
    print("Подготовка модов для песочниц…")
    downloads = os.path.join(work, "downloads")
    small, large = small_pack_files(downloads), large_pack_files(downloads)
    results = []
    for name in names:
        prepare(work, small, large)
        print(f"• {name} …", end=" ", flush=True)
        result = run_scenario(name, work)
        results.append(result)
        state = "пропущен" if result["skipped"] else "OK" if result["ok"] else "ОШИБКА"
        print(f"{state} ({result['seconds']:.0f} с)" + (f" — {result['skipped'][5:]}" if result["skipped"] else ""))
        for line in result["problems"]:
            print(f"    {line}")
    failed = [r for r in results if not r["ok"]]
    print(f"\nИтого: {len(results) - len(failed)} из {len(results)} без ошибок. Логи: {os.path.join(work, 'logs')}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
