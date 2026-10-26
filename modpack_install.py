"""
Установка готовой сборки из каталога (без Qt): .mrpack (Modrinth) или .zip с manifest.json (CurseForge).

    plan = load_plan(путь к файлу сборки)          — что внутри: версия игры, загрузчик, файлы, overrides
    target = prism_target(...) | official_target(...) | folder_target(...)
                                                    — куда ставить: новая сборка Prism, установка Minecraft Launcher
                                                      (Fabric/Quilt) или папка
    install(plan, target, progress, cancelled)     — скачать файлы с проверкой хэшей и распаковать overrides

Сборка ставится в новую пустую папку, поэтому настройки модов, ресурспаки и шейдеры из сборки (overrides)
распаковываются целиком — перезаписывать нечего. Для Prism создаётся экземпляр с нужными версиями
Minecraft, загрузчика и LWJGL (по справочнику Prism), и «Играть» работает сразу.
"""
import hashlib
import json
import logging
import os
import re
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from i18n import tr

ProgressCallback = Callable[[int, int, str], None]


class InstallCancelled(Exception):
    pass


@dataclass
class PackFile:
    path: str                 # Относительно папки игры: mods/x.jar, resourcepacks/y.zip
    url: Optional[str]        # None — автор запретил скачивание программами (CurseForge)
    hashes: Dict[str, str] = field(default_factory=dict)
    size: int = 0
    manual_url: str = ""
    name: str = ""


@dataclass
class PackPlan:
    name: str
    version: str
    game_version: Optional[str]
    loader: Optional[str]
    loader_version: Optional[str]
    files: List[PackFile]
    archive: str                      # Сам файл сборки (для overrides)
    overrides: List[str]              # Папки внутри архива, которые распаковываются в папку игры

    @property
    def manual(self) -> List[PackFile]:
        return [f for f in self.files if not f.url]


def safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "_", name).strip(" .") or "Modpack"


# --- Чтение сборки ---

def load_plan(archive: str) -> PackPlan:
    """План установки по файлу сборки. ValueError — непонятный файл."""
    from scanner import curseforge_pack_game_info, mrpack_game_info, read_curseforge_pack, read_mrpack
    if archive.lower().endswith(".mrpack"):
        index = read_mrpack(archive)
        game, loader = mrpack_game_info(index)
        deps = index.get("dependencies") or {}
        loader_version = next((deps[k] for k in ("fabric-loader", "quilt-loader", "neoforge", "forge") if k in deps), None)
        files = []
        for item in index["files"]:
            if (item.get("env") or {}).get("client") == "unsupported" or not item.get("downloads"):
                continue
            path = str(item.get("path", "")).replace("\\", "/")
            if not path or path.startswith("/") or ".." in path.split("/"):
                continue  # Путь за пределы папки игры — не ставим
            files.append(PackFile(path, item["downloads"][0], dict(item.get("hashes") or {}), int(item.get("fileSize") or 0),
                                  name=os.path.basename(path)))
        return PackPlan(index.get("name") or os.path.basename(archive), str(index.get("versionId") or ""), game, loader,
                        loader_version, files, archive, ["overrides/", "client-overrides/"])
    manifest = read_curseforge_pack(archive)
    game, loader = curseforge_pack_game_info(manifest)
    loaders = (manifest.get("minecraft") or {}).get("modLoaders") or []
    primary = next((l for l in loaders if l.get("primary")), loaders[0] if loaders else {})
    loader_version = str(primary.get("id") or "").split("-", 1)[1] if "-" in str(primary.get("id") or "") else None
    files = _curseforge_files(manifest)
    overrides = str(manifest.get("overrides") or "overrides").strip("/") + "/"
    return PackPlan(manifest.get("name") or os.path.basename(archive), str(manifest.get("version") or ""), game, loader,
                    loader_version, files, archive, [overrides])


def _curseforge_files(manifest: Dict) -> List[PackFile]:
    import curseforge
    if not curseforge.available():
        raise ValueError(tr("Для сборок CurseForge нужна программа с поддержкой CurseForge (ключ API)."))
    wanted = [f for f in manifest["files"] if f.get("fileID") and f.get("projectID") and f.get("required", True)]
    files = curseforge.get_files_by_ids(f["fileID"] for f in wanted)
    mods = curseforge.get_mods(f["projectID"] for f in wanted)
    folders = {curseforge.CLASS_MODS: "mods", 12: "resourcepacks", 6552: "shaderpacks"}
    result = []
    for item in wanted:
        file, mod = files.get(int(item["fileID"])), mods.get(int(item["projectID"]))
        if not file or not mod:
            continue
        folder = folders.get(mod.get("classId", curseforge.CLASS_MODS))
        if not folder:
            continue  # Датапаки и прочее ставятся в мир — в сборку не кладём
        version = curseforge.to_version(file, mod)
        sha1 = curseforge.file_sha1(file)
        result.append(PackFile(f"{folder}/{file['fileName']}", version["files"][0]["url"], {"sha1": sha1} if sha1 else {},
                               int(file.get("fileLength") or 0), version.get("manual_url", ""), mod.get("name") or ""))
    return result


# --- Куда ставить ---

@dataclass
class Target:
    game_dir: str
    instance_dir: Optional[str] = None  # Для Prism: папка экземпляра (в ней mmc-pack.json и instance.cfg)
    source: str = "manual"
    minecraft_dir: Optional[str] = None  # Для Minecraft Launcher: его .minecraft (versions, launcher_profiles.json)
    name: str = ""                       # Название установки в Minecraft Launcher
    launcher_id: str = ""                # Заполняется после установки: id новой установки лаунчера


def unique_dir(parent: str, name: str) -> str:
    base = os.path.join(parent, safe_name(name))
    path, n = base, 2
    while os.path.exists(path):
        path, n = f"{base} ({n})", n + 1
    return path


def folder_target(parent: str, name: str) -> Target:
    return Target(game_dir=unique_dir(parent, name))


def prism_target(instances_root: str, name: str, source: str = "prism") -> Target:
    instance = unique_dir(instances_root, name)
    return Target(game_dir=os.path.join(instance, "minecraft"), instance_dir=instance, source=source)


def official_target(minecraft_dir: str, name: str) -> Target:
    """Новая установка в Minecraft Launcher: своя папка игры в .minecraft/modpacks/<название>."""
    return Target(game_dir=unique_dir(os.path.join(minecraft_dir, "modpacks"), name), source="official",
                  minecraft_dir=minecraft_dir, name=name)


def write_prism_instance(target: Target, plan: PackPlan, session=None):
    """instance.cfg и mmc-pack.json новой сборки Prism: Minecraft, LWJGL, intermediary и загрузчик."""
    import launcher_pack
    os.makedirs(target.instance_dir, exist_ok=True)
    with open(os.path.join(target.instance_dir, "instance.cfg"), "w", encoding="utf-8") as f:
        f.write(f"[General]\nConfigVersion=1.3\nInstanceType=OneSix\nname={os.path.basename(target.instance_dir)}\n"
                "iconKey=default\nAutomaticJava=true\n")
    components = []
    requires = launcher_pack.fetch_requires(launcher_pack.MINECRAFT, plan.game_version, session)
    for need in requires:
        version = need.get("equals") or need.get("suggests")
        if version:
            components.append({"uid": need["uid"], "version": version, "dependencyOnly": True})
    components.append({"uid": launcher_pack.MINECRAFT, "version": plan.game_version, "important": True})
    if plan.loader in ("fabric", "quilt"):
        components.append({"uid": launcher_pack.INTERMEDIARY, "version": plan.game_version, "dependencyOnly": True})
    if plan.loader and plan.loader_version:
        components.append({"uid": launcher_pack.LOADER_UIDS[plan.loader], "version": plan.loader_version})
    with open(os.path.join(target.instance_dir, launcher_pack.PACK_NAME), "w", encoding="utf-8") as f:
        json.dump({"components": components, "formatVersion": 1}, f, indent=4)


# --- Установка ---

def _check_hash(path: str, hashes: Dict[str, str]) -> bool:
    for algo in ("sha512", "sha1"):
        expected = (hashes or {}).get(algo)
        if expected:
            digest = hashlib.new(algo)
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest().lower() == expected.lower()
    return True  # Хэша нет — проверить нечем


class _Meter:
    """Сколько скачано и с какой скоростью — для строки прогресса («12,3 из 845,0 МБ · 4,2 МБ/с»)."""
    WINDOW = 3.0  # Скорость считаем по последним секундам: видно, если загрузка встала

    def __init__(self, total_files: int, total_bytes: int, progress: ProgressCallback):
        import collections
        import threading
        import time
        self._time = time.monotonic
        self.lock = threading.Lock()
        self.total_files, self.total_bytes, self.progress = total_files, total_bytes, progress
        self.files = 0
        self.bytes = 0
        self.current = ""
        self.samples = collections.deque([(self._time(), 0)])
        self.reported = 0.0

    def add(self, count: int, name: str = "", file_done: bool = False):
        with self.lock:
            self.bytes += count
            self.files += 1 if file_done else 0
            self.current = name or self.current
            now = self._time()
            self.samples.append((now, self.bytes))
            while len(self.samples) > 2 and now - self.samples[0][0] > self.WINDOW:
                self.samples.popleft()
            if not file_done and now - self.reported < 0.25:
                return
            self.reported = now
            text = self.text()
        self.progress(self.files, self.total_files, text)

    def text(self) -> str:
        from view_model import format_size
        (start, first), (end, last) = self.samples[0], self.samples[-1]
        speed = (last - first) / (end - start) if end > start else 0
        total = format_size(max(self.total_bytes, self.bytes)) if self.total_bytes else "?"
        return tr("{0} · {1} из {2} · {3}/с", self.current, format_size(self.bytes), total, format_size(speed))


def download_files(jobs: List[tuple], progress: ProgressCallback, cancelled: Callable[[], bool], session,
                   workers: int = 4) -> Dict[str, str]:
    """
    Качает файлы сборки по несколько сразу: jobs — [(PackFile, куда сохранить)]. Проверяет контрольные суммы.
    Возвращает ошибки {путь в сборке: текст}; при отмене — InstallCancelled (недокачанное удаляется).
    """
    import concurrent.futures
    meter = _Meter(len(jobs), sum(item.size for item, _ in jobs), progress)
    stop = []

    def fetch(item: PackFile, destination: str):
        name = item.name or os.path.basename(item.path)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        response = session.get(item.url, timeout=60, stream=True)
        try:
            response.raise_for_status()
            with open(destination, "wb") as f:
                chunks = response.iter_content(256 * 1024) if hasattr(response, "iter_content") else [response.content]
                for chunk in chunks:
                    if stop or cancelled():
                        raise InstallCancelled()
                    f.write(chunk)
                    meter.add(len(chunk), name)
        except BaseException:
            if os.path.exists(destination):
                os.remove(destination)
            raise
        finally:
            close = getattr(response, "close", None)
            if close:
                close()
        if not _check_hash(destination, item.hashes):
            os.remove(destination)
            raise ValueError(tr("контрольная сумма не совпала"))
        meter.add(0, name, file_done=True)

    errors = {}
    progress(0, len(jobs), tr("Начинаем загрузку…"))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch, item, destination): item for item, destination in jobs}
        try:
            for future in concurrent.futures.as_completed(futures):
                item = futures[future]
                try:
                    future.result()
                except InstallCancelled:
                    raise
                except Exception as e:  # Один файл не скачался — остальные качаем
                    errors[item.path] = str(e)
                    logging.warning(tr("Сборка: не удалось скачать {0}: {1}", item.path, e))
        except InstallCancelled:
            stop.append(True)
            executor.shutdown(wait=True, cancel_futures=True)
            raise
    if cancelled():
        raise InstallCancelled()
    return errors


def install(plan: PackPlan, target: Target, progress: ProgressCallback = lambda d, t, s: None,
            cancelled: Callable[[], bool] = lambda: False, session=None) -> List[str]:
    """Ставит сборку. Возвращает ошибки (пустой список — всё скачано). Файлы «только с сайта» пропускаются."""
    from mod_updater_logic import SESSION
    session = session or SESSION
    version_id = None
    if target.minecraft_dir:  # Сначала загрузчик: если его справочник недоступен, моды качать незачем
        import official_launcher
        version_id = official_launcher.install_loader(target.minecraft_dir, plan.loader, plan.game_version,
                                                      plan.loader_version, session)
    os.makedirs(target.game_dir, exist_ok=True)
    if target.instance_dir:
        write_prism_instance(target, plan)
    files = [f for f in plan.files if f.url]
    jobs = [(f, os.path.join(target.game_dir, *f.path.split("/")) + ".part") for f in files]
    failed = download_files(jobs, progress, cancelled, session)
    for item, part in jobs:
        if item.path not in failed:
            os.replace(part, part[:-len(".part")])
    errors = [f"{path}: {error}" for path, error in failed.items()]
    progress(len(files), len(files), tr("Распаковываем настройки сборки…"))
    _extract_overrides(plan, target.game_dir)
    if version_id:
        import official_launcher
        target.launcher_id, _ = official_launcher.add_installation(target.minecraft_dir, target.name, version_id,
                                                                   target.game_dir)
    return errors


def _extract_overrides(plan: PackPlan, game_dir: str):
    """Настройки модов, ресурспаки и прочее из папки overrides архива — в папку игры."""
    root = os.path.realpath(game_dir)
    with zipfile.ZipFile(plan.archive) as archive:
        for member in archive.infolist():
            prefix = next((p for p in plan.overrides if member.filename.startswith(p)), None)
            if not prefix or member.is_dir():
                continue
            relative = member.filename[len(prefix):]
            destination = os.path.realpath(os.path.join(game_dir, relative))
            if not relative or not destination.startswith(root + os.sep):
                continue  # Защита от путей вида ../../ в архиве
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with archive.open(member) as source, open(destination, "wb") as out:
                out.write(source.read())


# --- Файл сборки из каталога ---

def latest_pack_file(project_id: str) -> Dict:
    """Последний выпуск сборки: {"url", "filename", "version"} (Modrinth .mrpack или CurseForge .zip)."""
    import curseforge
    from mod_updater_logic import MODRINTH_API_BASE, _get_json
    if curseforge.is_curseforge(project_id):
        files = curseforge._get(f"/mods/{curseforge.mod_id(project_id)}/files", {"pageSize": 30}).get("data") or []
        files = [f for f in files if not f.get("isServerPack")]
        files.sort(key=lambda f: (f.get("releaseType") != curseforge.RELEASE, -(_stamp(f.get("fileDate")))))
        if not files:
            raise ValueError(tr("У сборки нет файлов для скачивания"))
        file = files[0]
        if not file.get("downloadUrl"):
            raise ValueError(tr("Автор разрешает скачивать сборку только с сайта CurseForge"))
        return {"url": file["downloadUrl"], "filename": file["fileName"], "version": file.get("displayName") or ""}
    versions = _get_json(f"{MODRINTH_API_BASE}/project/{project_id}/version")
    if not versions:
        raise ValueError(tr("У сборки нет файлов для скачивания"))
    version = next((v for v in versions if v.get("version_type") == "release"), versions[0])
    primary = next((f for f in version["files"] if f.get("primary")), version["files"][0])
    return {"url": primary["url"], "filename": primary["filename"], "version": version.get("version_number") or ""}


def _stamp(date: Optional[str]) -> float:
    import datetime
    try:
        return datetime.datetime.fromisoformat(str(date).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def download_pack(info: Dict, folder: str, session=None) -> str:
    from mod_updater_logic import SESSION
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, safe_name(info["filename"]))
    response = (session or SESSION).get(info["url"], timeout=120)
    response.raise_for_status()
    with open(path, "wb") as f:
        f.write(response.content)
    return path


# --- Обновление поставленной сборки ---
#
# При установке рядом со сборкой запоминается, какие файлы принадлежат ей (STATE_FILE): файлы сборки и хэши
# распакованных настроек. Обновление меняет только их: моды, которые пользователь добавил сам, остаются,
# а настройку из сборки перезаписываем, только если пользователь её не менял. Выключенный мод сборки остаётся
# выключенным. Всё заменённое и удалённое уходит в бэкап, обновление можно отменить.

STATE_DIR = "ModUpdater_Backups"
STATE_FILE = "modpack.json"
UPDATES_DIR = "modpack_updates"
DISABLED = ".disabled"


def state_path(game_dir: str) -> str:
    return os.path.join(game_dir, STATE_DIR, STATE_FILE)


def _abs(game_dir: str, relative: str) -> str:
    return os.path.join(game_dir, *relative.split("/"))


def _on_disk(game_dir: str, relative: str) -> Optional[str]:
    """Как файл сборки лежит на диске: как есть, выключенным (.disabled) или его нет (None)."""
    for candidate in (relative, relative + DISABLED):
        if os.path.isfile(_abs(game_dir, candidate)):
            return candidate
    return None


def _sha1(path: str) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _override_members(plan: PackPlan) -> Dict[str, zipfile.ZipInfo]:
    """Файлы overrides архива: путь в папке игры → запись архива (client-overrides важнее overrides)."""
    result = {}
    with zipfile.ZipFile(plan.archive) as archive:
        members = archive.infolist()
    for prefix in plan.overrides:
        for member in members:
            if member.filename.startswith(prefix) and not member.is_dir():
                relative = member.filename[len(prefix):]
                if relative and not relative.startswith("/") and ".." not in relative.split("/"):
                    result[relative] = member
    return result


def save_state(plan: PackPlan, project_id: str, game_dir: str, title: str = ""):
    """Запоминает поставленную сборку: из какого проекта, какая версия и какие файлы — её."""
    overrides = {}
    for relative in _override_members(plan):
        if os.path.isfile(_abs(game_dir, relative)):
            overrides[relative] = _sha1(_abs(game_dir, relative))
    state = {"project_id": project_id, "title": title or plan.name, "version": plan.version,
             "game_version": plan.game_version, "loader": plan.loader, "loader_version": plan.loader_version,
             "files": [f.path for f in plan.files], "overrides": overrides}
    os.makedirs(os.path.dirname(state_path(game_dir)), exist_ok=True)
    with open(state_path(game_dir), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def load_state(game_dir: str) -> Optional[Dict]:
    """Сборка из каталога, поставленная программой, или None."""
    try:
        with open(state_path(game_dir), "r", encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) and state.get("project_id") else None
    except (OSError, ValueError):
        return None


@dataclass
class UpdatePlan:
    download: List[PackFile]          # Новые и изменившиеся файлы сборки
    remove: List[str]                 # Файлы старой версии, которых в новой нет (как лежат на диске)
    overrides: List[str]              # Настройки, которые запишем: новые или не изменённые пользователем
    kept: List[str]                   # Настройки, которые пользователь менял, — остаются его
    user_mods: List[str]              # Моды, добавленные пользователем, — не трогаем

    @property
    def empty(self) -> bool:
        return not (self.download or self.remove or self.overrides)


def plan_update(state: Dict, plan: PackPlan, game_dir: str) -> UpdatePlan:
    old_files = set(state.get("files") or [])
    new_paths = {f.path for f in plan.files}
    download = []
    for item in plan.files:
        if not item.url:
            continue
        current = _on_disk(game_dir, item.path)
        if not current or not item.hashes or not _check_hash(_abs(game_dir, current), item.hashes):
            download.append(item)
    remove = sorted(filter(None, (_on_disk(game_dir, p) for p in old_files - new_paths)))
    known = state.get("overrides") or {}
    write, kept = [], []
    members = _override_members(plan)
    with zipfile.ZipFile(plan.archive) as archive:
        for relative in sorted(members):
            path = _abs(game_dir, relative)
            if not os.path.isfile(path):
                write.append(relative)
                continue
            current = _sha1(path)
            if current == hashlib.sha1(archive.read(members[relative])).hexdigest():
                continue  # В новой версии та же настройка
            (write if known.get(relative) == current else kept).append(relative)
    user_mods = []
    mods_dir = os.path.join(game_dir, "mods")
    if os.path.isdir(mods_dir):
        for name in sorted(os.listdir(mods_dir)):
            relative = "mods/" + (name[:-len(DISABLED)] if name.endswith(DISABLED) else name)
            if relative.endswith(".jar") and relative not in old_files and relative not in new_paths:
                user_mods.append(name)
    return UpdatePlan(download, remove, write, kept, user_mods)


def apply_update(update: UpdatePlan, plan: PackPlan, game_dir: str, progress: ProgressCallback = lambda d, t, s: None,
                 cancelled: Callable[[], bool] = lambda: False, session=None) -> str:
    """
    Обновляет сборку по плану и возвращает папку бэкапа (для undo_update). Если хоть один файл не скачался,
    или обновление отменили, всё уже сделанное откатывается и сборка остаётся прежней.
    """
    import datetime
    import shutil
    from mod_updater_logic import SESSION
    session = session or SESSION
    previous = load_state(game_dir) or {}
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
    backup = os.path.join(game_dir, STATE_DIR, UPDATES_DIR, stamp)
    os.makedirs(backup)
    journal = {"created": stamp, "from": previous.get("version"), "to": plan.version,
               "added": [], "backed_up": [], "state": previous}

    def save_journal():
        with open(os.path.join(backup, "journal.json"), "w", encoding="utf-8") as f:
            json.dump(journal, f, ensure_ascii=False, indent=1)

    def move_aside(relative: str):
        if os.path.exists(_abs(game_dir, relative)):
            target = _abs(backup, relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.move(_abs(game_dir, relative), target)
            journal["backed_up"].append(relative)

    def put(relative: str, data: bytes):
        current = _on_disk(game_dir, relative)
        if current:
            move_aside(current)
            relative = current  # Выключенный пользователем мод остаётся выключенным
        destination = _abs(game_dir, relative)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, "wb") as f:
            f.write(data)
        journal["added"].append(relative)

    save_journal()
    try:
        # Сначала всё скачиваем — в папке игры пока ничего не меняется
        jobs = [(item, os.path.join(backup, f"{i}.part")) for i, item in enumerate(update.download)]
        failed = download_files(jobs, progress, cancelled, session)
        if failed:
            raise ValueError("\n".join(f"{path}: {error}" for path, error in list(failed.items())[:12]))
        downloaded = [(item.path, part) for item, part in jobs]
        progress(len(update.download), len(update.download), tr("Обновляем файлы сборки…"))
        for relative, part in downloaded:
            with open(part, "rb") as f:
                put(relative, f.read())
            os.remove(part)
        for relative in update.remove:
            move_aside(relative)
        members = _override_members(plan)
        with zipfile.ZipFile(plan.archive) as archive:
            for relative in update.overrides:
                with archive.open(members[relative]) as source:
                    put(relative, source.read())
        save_journal()
    except BaseException:
        save_journal()
        undo_update(backup, game_dir)
        raise
    save_state(plan, previous.get("project_id", ""), game_dir, previous.get("title", ""))
    return backup


def undo_update(backup: str, game_dir: str):
    """Возвращает сборку к состоянию до обновления: новые файлы удаляются, прежние возвращаются из бэкапа."""
    import shutil
    with open(os.path.join(backup, "journal.json"), "r", encoding="utf-8") as f:
        journal = json.load(f)
    for relative in journal.get("added", []):
        if os.path.isfile(_abs(game_dir, relative)):
            os.remove(_abs(game_dir, relative))
    for relative in journal.get("backed_up", []):
        if os.path.exists(_abs(backup, relative)):
            os.makedirs(os.path.dirname(_abs(game_dir, relative)), exist_ok=True)
            shutil.move(_abs(backup, relative), _abs(game_dir, relative))
    if journal.get("state"):
        with open(state_path(game_dir), "w", encoding="utf-8") as f:
            json.dump(journal["state"], f, ensure_ascii=False, indent=1)
    shutil.rmtree(backup, ignore_errors=True)


def check_update(game_dir: str) -> Optional[Dict]:
    """Вышла ли новая версия сборки: {"state", "latest"} или None (сборка не из каталога или уже новая)."""
    state = load_state(game_dir)
    if not state:
        return None
    latest = latest_pack_file(state["project_id"])
    if latest.get("version") and latest["version"] != state.get("version"):
        return {"state": state, "latest": latest}
    return None
