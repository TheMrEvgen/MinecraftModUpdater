"""
Установка готовой сборки из каталога (без Qt): .mrpack (Modrinth) или .zip с manifest.json (CurseForge).

    plan = load_plan(путь к файлу сборки)          — что внутри: версия игры, загрузчик, файлы, overrides
    target = prism_target(...) | official_target(...) | folder_target(...)
                                                    — куда ставить: новая сборка Prism, установка Minecraft Launcher
                                                      (Fabric/Quilt) или папка
    install(plan, target, progress, cancelled)     — скачать файлы с проверкой хэшей и распаковать overrides

Сборка ставится в новую папку или прямо в выбранную пользователем; настройки модов, ресурспаки, шейдеры и миры из
сборки (overrides) распаковываются целиком. Карты, которые сборка CurseForge подключает отдельными проектами
(класс «Worlds»), скачиваются и распаковываются в saves. Для Prism создаётся экземпляр с нужными версиями
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


class _Paused(Exception):
    """Загрузку поставили на паузу: соединение закрываем, файл потом докачиваем с того же места."""


NETWORK_RETRIES = 3  # Обрыв связи посреди файла: столько раз докачиваем с места обрыва, потом — ошибка


@dataclass
class PackFile:
    path: str                 # Относительно папки игры: mods/x.jar, resourcepacks/y.zip
    url: Optional[str]        # None — автор запретил скачивание программами (CurseForge)
    hashes: Dict[str, str] = field(default_factory=dict)
    size: int = 0
    manual_url: str = ""
    name: str = ""
    world: bool = False       # Карта (мир CurseForge): скачанный .zip распаковывается в saves, сам архив удаляется
    datapack: bool = False    # Датапак: лежит в datapacks/ папки игры и копируется во все миры сборки


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
    kind: str = "mrpack"              # mrpack — Modrinth, curseforge — .zip с manifest.json
    source_file: Optional[Dict] = None  # Выпуск из каталога: pack_file() — {"url", "filename", "version", "id"}
    substitutes: Dict = field(default_factory=dict)  # Похожие файлы на Modrinth для «только с сайта» (выбор за нами)
    from_modrinth: int = 0            # Сколько файлов «только с сайта» нашлось на Modrinth теми же самыми

    @property
    def manual(self) -> List[PackFile]:
        return [f for f in self.files if not f.url]

    @property
    def datapacks(self) -> List[PackFile]:
        return [f for f in self.files if f.datapack]

    def size(self) -> int:
        """Сколько места займёт сборка, байт: файлы, распакованные overrides и карты (архив карты — примерно вдвое)."""
        total = sum(f.size * (2 if f.world else 1) for f in self.files)
        if self.archive and os.path.isfile(self.archive):
            try:
                with zipfile.ZipFile(self.archive) as archive:
                    total += sum(m.file_size for m in archive.infolist()
                                 if any(m.filename.startswith(p) for p in self.overrides))
            except zipfile.BadZipFile:
                pass
        return total


def versions_plan(name: str, game_version: str, loader: Optional[str], loader_version: Optional[str]) -> PackPlan:
    """План без файлов: только версии игры и загрузчика (перенос сборки в другой лаунчер)."""
    return PackPlan(name, "", game_version, loader, loader_version, [], "", [])


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
                    loader_version, files, archive, [overrides], kind="curseforge")


def _curseforge_files(manifest: Dict) -> List[PackFile]:
    import curseforge
    if not curseforge.available():
        raise ValueError(tr("Для сборок CurseForge нужна программа с поддержкой CurseForge (ключ API)."))
    wanted = [f for f in manifest["files"] if f.get("fileID") and f.get("projectID") and f.get("required", True)]
    files = curseforge.get_files_by_ids(f["fileID"] for f in wanted)
    mods = curseforge.get_mods(f["projectID"] for f in wanted)
    folders = {curseforge.CLASS_MODS: "mods", 12: "resourcepacks", 6552: "shaderpacks", CLASS_WORLDS: "saves",
               CLASS_DATAPACKS: DATAPACKS_DIR}
    result = []
    for item in wanted:
        file, mod = files.get(int(item["fileID"])), mods.get(int(item["projectID"]))
        if not file or not mod:
            continue
        class_id = mod.get("classId", curseforge.CLASS_MODS)
        folder = folders.get(class_id)
        if not folder:
            continue  # Прочие классы (дополнения к другим играм и т.п.) к Minecraft не относятся
        version = curseforge.to_version(file, mod)
        sha1 = curseforge.file_sha1(file)
        result.append(PackFile(f"{folder}/{file['fileName']}", version["files"][0]["url"], {"sha1": sha1} if sha1 else {},
                               int(file.get("fileLength") or 0), version.get("manual_url", ""), mod.get("name") or "",
                               world=class_id == CLASS_WORLDS, datapack=class_id == CLASS_DATAPACKS))
    return result


CLASS_WORLDS = 17       # Класс CurseForge «Worlds» — карты, которые автор сборки положил в неё
CLASS_DATAPACKS = 6945  # Класс CurseForge «Data Packs»
DATAPACKS_DIR = "datapacks"  # Датапаки сборки: общая папка в папке игры, оттуда — копии в каждый мир


def spread_datapacks(plan: PackPlan, game_dir: str) -> List[str]:
    """
    Кладёт датапаки сборки во все её миры (saves/<мир>/datapacks), где их ещё нет. Новые миры игра не трогает —
    датапак из папки datapacks добавляется при создании мира кнопкой «Пакеты данных». Возвращает миры.
    """
    from content import list_worlds
    names = [os.path.basename(f.path) for f in plan.datapacks]
    names = [n for n in names if os.path.isfile(os.path.join(game_dir, DATAPACKS_DIR, n))]
    if not names:
        return []
    import shutil
    worlds = list_worlds(game_dir)
    for world in worlds:
        folder = os.path.join(game_dir, "saves", world, "datapacks")
        os.makedirs(folder, exist_ok=True)
        for name in names:
            if not os.path.exists(os.path.join(folder, name)):
                shutil.copy2(os.path.join(game_dir, DATAPACKS_DIR, name), os.path.join(folder, name))
    return worlds


def unpack_world(archive: str, saves_dir: str, file_name: str = "") -> List[str]:
    """
    Распаковывает карту из .zip в saves. Внутри архива мир лежит либо в своей папке (Карта/level.dat), либо
    прямо в корне (level.dat) — тогда папкой мира становится имя файла карты (file_name, иначе имя архива).
    Существующие миры не перезаписываются: в них уже могли играть. Возвращает созданные папки миров.
    """
    os.makedirs(saves_dir, exist_ok=True)
    root = os.path.realpath(saves_dir)
    created = []
    with zipfile.ZipFile(archive) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        names = [m.filename.replace("\\", "/") for m in members]
        flat = "level.dat" in names
        base = safe_name(os.path.splitext(file_name or os.path.basename(archive))[0]) if flat else ""
        skipped = set()
        for member, name in zip(members, names):
            relative = f"{base}/{name}" if flat else name
            top = relative.split("/", 1)[0]
            if top in skipped:
                continue
            destination = os.path.realpath(os.path.join(saves_dir, *relative.split("/")))
            if not destination.startswith(root + os.sep) or "/" not in relative:
                continue  # Пути вида ../ и одиночные файлы рядом с мирами не распаковываем
            world_dir = os.path.join(saves_dir, top)
            if top not in created:
                if os.path.exists(world_dir):
                    skipped.add(top)
                    logging.info(tr("Сборка: мир «{0}» уже есть — не перезаписываем.", top))
                    continue
                created.append(top)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with zf.open(member) as source, open(destination, "wb") as out:
                out.write(source.read())
    return [os.path.join(saves_dir, top) for top in created]


# --- Куда ставить ---

@dataclass
class Target:
    game_dir: str
    instance_dir: Optional[str] = None  # Для Prism: папка экземпляра (в ней mmc-pack.json и instance.cfg)
    source: str = "manual"
    minecraft_dir: Optional[str] = None  # Для Minecraft Launcher: его .minecraft (versions, launcher_profiles.json)
    name: str = ""                       # Название установки в Minecraft Launcher
    launcher_id: str = ""                # Заполняется после установки: id новой установки лаунчера
    fresh: bool = True                   # Папку создали для сборки: при отмене её можно удалить целиком
    loader_here: bool = False            # Папка: поставить загрузчик в её versions (Legacy Launcher и т.п.)
    version_id: str = ""                 # Заполняется после установки: id версии загрузчика в versions
    modrinth_root: str = ""              # Modrinth App: его папка данных — записать профиль в app.db
    curseforge_zip: str = ""             # CurseForge App: собрать архив для импорта (сюда), а не ставить


# Куда можно поставить сборку: ключ → название. Какие из них есть на компьютере — available_launchers()
LAUNCHER_TITLES = {"prism": "Prism Launcher", "multimc": "MultiMC", "official": "Minecraft Launcher",
                   "modrinth": "Modrinth App", "curseforge": "CurseForge App"}


def available_launchers() -> Dict[str, str]:
    """Лаунчеры на этом компьютере: ключ → папка (экземпляров Prism/MultiMC, .minecraft, данных Modrinth App…)."""
    import modrinth_app
    import official_launcher
    from launchers import _appdata, _prism_instance_dir
    appdata = _appdata()
    result = {}
    for key, folder in (("prism", "PrismLauncher"), ("multimc", "MultiMC")):
        root = os.path.join(appdata, folder)
        if os.path.isdir(root):
            result[key] = _prism_instance_dir(root)
    minecraft = official_launcher.minecraft_dir()
    if os.path.isdir(minecraft):
        result["official"] = minecraft
    modrinth = modrinth_app.find_root()
    if modrinth:
        result["modrinth"] = modrinth
    curseforge = os.path.join(os.path.expanduser("~"), "curseforge", "minecraft")
    if os.path.isdir(curseforge):
        result["curseforge"] = curseforge
    return result


def downloads_folder() -> str:
    folder = os.path.join(os.path.expanduser("~"), "Downloads")
    return folder if os.path.isdir(folder) else os.path.expanduser("~")


def make_target(kind: str, root: str, name: str) -> Target:
    """Новая сборка в лаунчере kind (ключ LAUNCHER_TITLES); root — его папка из available_launchers()."""
    if kind in ("prism", "multimc"):
        return prism_target(root, name, kind)
    if kind == "official":
        return official_target(root, name)
    if kind == "modrinth":
        return Target(game_dir=unique_dir(os.path.join(root, "profiles"), name), source="modrinth",
                      modrinth_root=root, name=name)
    if kind == "curseforge":  # Ставим во временную папку и упаковываем в архив для импорта
        import config as app_config
        return Target(game_dir=unique_dir(os.path.join(app_config.CONFIG_DIR, "staging"), name), source="curseforge",
                      name=name, curseforge_zip=os.path.join(downloads_folder(), f"{safe_name(name)} (CurseForge).zip"))
    raise ValueError(tr("Неизвестный лаунчер: {0}", kind))


def unique_dir(parent: str, name: str) -> str:
    base = os.path.join(parent, safe_name(name))
    path, n = base, 2
    while os.path.exists(path):
        path, n = f"{base} ({n})", n + 1
    return path


def folder_target(parent: str, name: str, own_folder: bool = True, loader_here: bool = False) -> Target:
    """own_folder — создать внутри parent папку с названием сборки; иначе ставить прямо в parent.
    loader_here — поставить загрузчик в папку versions этой папки игры."""
    if own_folder:
        return Target(game_dir=unique_dir(parent, name), loader_here=loader_here, name=name)
    return Target(game_dir=parent, fresh=folder_is_empty(parent), loader_here=loader_here, name=name)


def looks_like_launcher_dir(path: str) -> bool:
    """Папка игры, в которой лаунчер держит и версии (как .minecraft): туда загрузчик ставится в versions."""
    return any(os.path.exists(os.path.join(path, n)) for n in ("versions", "launcher_profiles.json"))


def free_space(path: str) -> Optional[int]:
    """Свободно на диске, где будет path (байт); None — не узнать."""
    import shutil
    probe = os.path.abspath(path)
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def folder_is_empty(path: str) -> bool:
    return not os.path.isdir(path) or not os.listdir(path)


def folder_has_mods(path: str) -> bool:
    mods = os.path.join(path, "mods")
    return os.path.isdir(mods) and any(n.lower().endswith((".jar", ".jar.disabled")) for n in os.listdir(mods))


def prism_target(instances_root: str, name: str, source: str = "prism") -> Target:
    instance = unique_dir(instances_root, name)
    return Target(game_dir=os.path.join(instance, "minecraft"), instance_dir=instance, source=source)


def official_target(minecraft_dir: str, name: str) -> Target:
    """Новая установка в Minecraft Launcher: своя папка игры в .minecraft/modpacks/<название>."""
    return Target(game_dir=unique_dir(os.path.join(minecraft_dir, "modpacks"), name), source="official",
                  minecraft_dir=minecraft_dir, name=name)


# Настройки новой сборки Prism — те же поля, что Prism пишет сам (Java и память — автоматически, по умолчанию)
PRISM_CFG_DEFAULTS = (
    ("AutomaticJava", "true"), ("ExportAuthor", ""), ("ExportName", ""), ("ExportOptionalFiles", "true"),
    ("ExportSummary", ""), ("ExportVersion", "1.0.0"), ("GlobalDataPacksEnabled", "false"), ("GlobalDataPacksPath", ""),
    ("IgnoreJavaCompatibility", "false"), ("JoinServerOnLaunch", "false"), ("LogPrePostOutput", "true"),
    ("ManagedPack", "false"), ("ManagedPackID", ""), ("ManagedPackName", ""), ("ManagedPackType", ""),
    ("ManagedPackURL", ""), ("ManagedPackVersionID", ""), ("ManagedPackVersionName", ""), ("ModDownloadLoaders", "[]"),
    ("OverrideCommands", "false"), ("OverrideConsole", "false"), ("OverrideEnv", "false"),
    ("OverrideGameTime", "false"), ("OverrideJavaArgs", "false"), ("OverrideJavaLocation", "false"),
    ("OverrideLegacySettings", "false"), ("OverrideMemory", "false"), ("OverrideMiscellaneous", "false"),
    ("OverrideModDownloadLoaders", "false"), ("OverrideNativeWorkarounds", "false"), ("OverridePerformance", "false"),
    ("OverrideWindow", "false"), ("Profiler", ""), ("UseAccountForInstance", "false"), ("lastLaunchTime", "0"),
    ("lastTimePlayed", "0"), ("linkedInstances", "[]"), ("notes", ""), ("shortcuts", ""), ("totalTimePlayed", "0"),
)


def prism_instance_cfg(name: str) -> str:
    """instance.cfg новой сборки Prism / MultiMC."""
    import uuid
    name = " ".join(str(name).split()) or "Modpack"  # Без переводов строк: это одна строка INI
    lines = ["[General]", "ConfigVersion=1.3", "InstanceType=OneSix", "iconKey=default", f"name={name}"]
    lines += [f"{key}={value}" for key, value in PRISM_CFG_DEFAULTS]
    lines.append(f"uuid={uuid.uuid4().hex}")
    return "\n".join(lines) + "\n"


def _prism_component(uid: str, version: str, session=None, **flags) -> Dict:
    """Компонент mmc-pack.json с теми же сведениями из справочника Prism, что лаунчер хранит сам (cached*)."""
    import launcher_pack
    component = {"uid": uid, "version": version, **flags}
    try:
        meta = launcher_pack.fetch_meta(uid, version, session)
    except Exception as e:  # Без сведений сборка тоже откроется — Prism допишет их сам
        logging.debug(f"Справочник Prism: {uid} {version}: {e}")
        return component
    component["cachedName"] = meta.get("name") or uid
    component["cachedVersion"] = version
    if meta.get("requires"):
        component["cachedRequires"] = meta["requires"]
    if meta.get("volatile"):
        component["cachedVolatile"] = True
    return component


def _prism_staging(instances_root: str) -> str:
    """
    Временная папка для новой сборки — рядом с instances, на том же диске (переименование мгновенное). Внутри
    instances нельзя: Prism считает сборкой любую папку там, даже с точкой в имени, и пишет в неё свой instance.cfg.
    """
    import uuid
    name = f".modupdater-new-{uuid.uuid4().hex[:12]}"
    parent = os.path.dirname(os.path.normpath(instances_root))
    if parent and parent != os.path.normpath(instances_root):
        try:
            probe = os.path.join(parent, name)
            os.makedirs(probe)
            os.rmdir(probe)
            if os.stat(parent).st_dev == os.stat(instances_root if os.path.isdir(instances_root) else parent).st_dev:
                return os.path.join(parent, name)
        except OSError:
            pass
    return os.path.join(instances_root, name)  # Рядом нельзя (нет прав) — внутри, как раньше


def write_prism_instance(target: Target, plan: PackPlan, session=None):
    """
    Новая сборка Prism / MultiMC: instance.cfg и mmc-pack.json (Minecraft, LWJGL, intermediary и загрузчик).
    Сборка собирается во временной папке рядом с instances и появляется в них одним переименованием — уже с
    настройками. Иначе Prism, который следит за папкой, увидит пустую папку, пропустит её и покажет сборку только
    после перезапуска.
    """
    import shutil

    import launcher_pack
    requires = launcher_pack.fetch_requires(launcher_pack.MINECRAFT, plan.game_version, session)  # Есть ли версия
    components = []
    for need in requires:
        version = need.get("equals") or need.get("suggests")
        if version:
            components.append(_prism_component(need["uid"], version, session, dependencyOnly=True))
    minecraft = _prism_component(launcher_pack.MINECRAFT, plan.game_version, session, important=True)
    minecraft.setdefault("cachedRequires", requires)
    components.append(minecraft)
    if plan.loader in ("fabric", "quilt"):
        components.append(_prism_component(launcher_pack.INTERMEDIARY, plan.game_version, session, dependencyOnly=True))
    if plan.loader and plan.loader_version:
        version = plan.loader_version
        if plan.loader == "forge":  # В справочнике Prism версия Forge — без версии игры: «47.2.0»
            import official_launcher
            version = official_launcher.forge_version(plan.game_version, version)
        components.append(_prism_component(launcher_pack.LOADER_UIDS[plan.loader], version, session))

    root = os.path.dirname(os.path.normpath(target.instance_dir))
    os.makedirs(root, exist_ok=True)
    game_name = os.path.basename(os.path.normpath(target.game_dir))
    staging = _prism_staging(root)
    os.makedirs(os.path.join(staging, game_name))
    try:
        with open(os.path.join(staging, "instance.cfg"), "w", encoding="utf-8", newline="\n") as f:
            f.write(prism_instance_cfg(target.name or plan.name or os.path.basename(target.instance_dir)))
        with open(os.path.join(staging, launcher_pack.PACK_NAME), "w", encoding="utf-8") as f:
            json.dump({"components": components, "formatVersion": 1}, f, indent=4, ensure_ascii=False)
        if os.path.exists(target.instance_dir):  # Пока качали справочник, папку с таким именем заняли
            target.instance_dir = unique_dir(root, os.path.basename(os.path.normpath(target.instance_dir)))
            target.game_dir = os.path.join(target.instance_dir, game_name)
        os.rename(staging, target.instance_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


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
                   workers: int = 4, job=None) -> Dict[str, str]:
    """
    Качает файлы сборки по несколько сразу: jobs — [(PackFile, куда сохранить)]. Проверяет контрольные суммы.
    Возвращает ошибки {путь в сборке: текст}; при отмене — InstallCancelled (недокачанное удаляется).
    job — downloads.DownloadJob: туда же сообщается о каждом файле (страница «Загрузки»).
    """
    import concurrent.futures
    meter = _Meter(len(jobs), sum(item.size for item, _ in jobs), progress)
    stop = []
    if job:
        for item, _ in jobs:
            if item.path not in job.files:  # При повторе файлы уже в списке загрузки
                job.add_file(item.path, item.name or os.path.basename(item.path), item.size)

    def fetch(item: PackFile, destination: str):
        import time

        import requests
        name = item.name or os.path.basename(item.path)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        if os.path.exists(destination):
            os.remove(destination)  # Остаток прошлой попытки
        written, attempts = 0, 0
        try:
            while True:
                if stop or cancelled():
                    raise InstallCancelled()
                if job:
                    job.wait_if_paused(lambda: bool(stop) or cancelled())
                    job.file_started(item.path)
                response = None
                try:
                    # Докачка: просим у сервера только недостающее (если он не умеет — вернёт файл целиком)
                    response = session.get(item.url, timeout=60, stream=True,
                                           headers={"Range": f"bytes={written}-"} if written else None)
                    response.raise_for_status()
                    if written and getattr(response, "status_code", 200) != 206:
                        written = 0
                    headers = getattr(response, "headers", None) or {}
                    size = item.size or (int(headers.get("Content-Length") or 0) + written)
                    with open(destination, "ab" if written else "wb") as f:
                        chunks = (response.iter_content(256 * 1024) if hasattr(response, "iter_content")
                                  else [response.content])
                        for chunk in chunks:
                            if stop or cancelled():
                                raise InstallCancelled()
                            if job and job.paused:
                                raise _Paused()
                            f.write(chunk)
                            written += len(chunk)
                            meter.add(len(chunk), name)
                            if job:
                                job.file_progress(item.path, written, size)
                    break
                except _Paused:
                    continue
                except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as e:
                    attempts += 1
                    if attempts > NETWORK_RETRIES:
                        raise
                    logging.info(tr("Сборка: обрыв связи на {0} ({1}), докачиваем…", name, e))
                    time.sleep(min(8, 2 ** attempts))
                finally:
                    close = getattr(response, "close", None)
                    if close:
                        close()
        except BaseException:
            if os.path.exists(destination):
                os.remove(destination)
            raise
        if not _check_hash(destination, item.hashes):
            os.remove(destination)
            raise ValueError(tr("контрольная сумма не совпала"))
        meter.add(0, name, file_done=True)
        if job:
            job.file_done(item.path)

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
                    if job:
                        job.file_done(item.path, str(e))
                    logging.warning(tr("Сборка: не удалось скачать {0}: {1}", item.path, e))
        except InstallCancelled:
            stop.append(True)
            executor.shutdown(wait=True, cancel_futures=True)
            raise
    if cancelled():
        raise InstallCancelled()
    return errors


def install(plan: PackPlan, target: Target, progress: ProgressCallback = lambda d, t, s: None,
            cancelled: Callable[[], bool] = lambda: False, session=None, job=None) -> List[str]:
    """Ставит сборку. Возвращает ошибки (пустой список — всё скачано). Файлы «только с сайта» пропускаются."""
    import shutil

    from mod_updater_logic import SESSION
    session = session or SESSION
    if target.curseforge_zip and plan.kind == "curseforge":
        # Сборка и так в формате CurseForge: приложение само скачает моды при импорте
        os.makedirs(os.path.dirname(target.curseforge_zip), exist_ok=True)
        shutil.copy2(plan.archive, target.curseforge_zip)
        return []
    prepare_target(target, plan, session, lambda text: progress(0, len(plan.files), text))
    files = [f for f in plan.files if f.url]
    errors = download_missing(plan, target.game_dir, [f.path for f in files], progress, cancelled, session, job)
    progress(len(files), len(files), tr("Распаковываем настройки сборки…"))
    _extract_overrides(plan, target.game_dir)
    spread_datapacks(plan, target.game_dir)
    if target.curseforge_zip:  # Архив для импорта в CurseForge App: всё скачанное — в overrides
        import exporters
        progress(len(files), len(files), tr("Упаковываем архив для CurseForge App…"))
        manifest = exporters.build_curseforge_manifest(plan.name, plan.version, plan.game_version, plan.loader,
                                                       plan.loader_version or "", [])
        overrides = [(os.path.join(target.game_dir, n), n) for n in sorted(os.listdir(target.game_dir))]
        try:
            exporters.write_curseforge_pack(target.curseforge_zip, manifest, overrides)
        finally:
            shutil.rmtree(target.game_dir, ignore_errors=True)
        return errors
    register_target(target, plan)
    return errors


def prepare_target(target: Target, plan: PackPlan, session=None, progress=lambda text: None):
    """
    До файлов сборки: загрузчик (Minecraft Launcher или папка versions) и описание экземпляра лаунчера (Prism,
    MultiMC). Сначала загрузчик: если его не поставить, качать моды незачем. Modrinth App проверяется заранее.
    """
    import official_launcher
    if target.modrinth_root:
        import modrinth_app
        modrinth_app.check(target.modrinth_root)
    loader_dir = target.minecraft_dir or (target.game_dir if target.loader_here else None)
    if loader_dir and plan.loader:
        if not plan.loader_version:
            raise ValueError(tr("Не удалось узнать версию загрузчика {0} для Minecraft {1}", plan.loader,
                                plan.game_version))
        target.version_id = official_launcher.install_loader(loader_dir, plan.loader, plan.game_version,
                                                             plan.loader_version, session, progress)
    if target.instance_dir:  # Prism / MultiMC: сборка появляется в лаунчере сразу целиком
        write_prism_instance(target, plan, session)
    os.makedirs(target.game_dir, exist_ok=True)


def transfer(game_dir: str, kind: str, root: str, name: str, game_version: str, loader: Optional[str],
             loader_version: Optional[str], keep_source: bool = False, session=None,
             progress=lambda text: None) -> Target:
    """
    Переносит сборку из папки game_dir в новую сборку лаунчера kind: создаёт экземпляр с той же версией игры и
    загрузчиком, переносит (или копирует) моды, настройки и миры и регистрирует сборку в лаунчере.
    CurseForge App — архив для импорта (старая сборка остаётся на месте). Возвращает новую цель.
    """
    import shutil

    import exporters
    from maintenance import move_game_dir, movable_items
    from mod_updater_logic import get_latest_loader_version
    if loader and not loader_version:
        loader_version = get_latest_loader_version(loader, game_version)
    plan = versions_plan(name, game_version, loader, loader_version)
    target = make_target(kind, root, name)
    if target.curseforge_zip:
        progress(tr("Упаковываем архив для CurseForge App…"))
        manifest = exporters.build_curseforge_manifest(name, "", game_version, loader or "", loader_version or "", [])
        os.makedirs(os.path.dirname(target.curseforge_zip), exist_ok=True)
        exporters.write_curseforge_pack(target.curseforge_zip, manifest,
                                        [(os.path.join(game_dir, n), n) for n in movable_items(game_dir)])
        return target
    try:
        prepare_target(target, plan, session, progress)
    except BaseException:
        shutil.rmtree(target.instance_dir or target.game_dir, ignore_errors=True)  # Новая папка — убираем начатое
        raise
    progress(tr("Копируем сборку…") if keep_source else tr("Переносим сборку…"))
    move_game_dir(game_dir, target.game_dir, keep_source)
    register_target(target, plan)
    return target


def register_target(target: Target, plan: PackPlan):
    """После файлов сборки: новая установка в Minecraft Launcher, профиль в Modrinth App."""
    if target.minecraft_dir and target.version_id:
        import official_launcher
        target.launcher_id, _ = official_launcher.add_installation(target.minecraft_dir, target.name or plan.name,
                                                                   target.version_id, target.game_dir)
    if target.modrinth_root:
        import modrinth_app
        modrinth_app.add_profile(target.modrinth_root, target.game_dir, target.name or plan.name, plan.game_version,
                                 plan.loader, plan.loader_version)


def download_missing(plan: PackPlan, game_dir: str, paths: List[str], progress: ProgressCallback = lambda d, t, s: None,
                     cancelled: Callable[[], bool] = lambda: False, session=None, job=None) -> List[str]:
    """Качает файлы сборки paths в папку игры (при установке — все, при «Повторить» — неудавшиеся). Возвращает ошибки."""
    from mod_updater_logic import SESSION
    wanted = set(paths)
    jobs = [(f, _abs(game_dir, f.path) + ".part") for f in plan.files if f.url and f.path in wanted]
    failed = download_files(jobs, progress, cancelled, session or SESSION, job=job)
    for item, part in jobs:
        if item.path in failed:
            continue
        if item.world:  # Карта: распаковываем в saves, архив не нужен
            try:
                unpack_world(part, os.path.join(game_dir, "saves"), os.path.basename(item.path))
            except (OSError, zipfile.BadZipFile) as e:
                failed[item.path] = str(e)
            finally:
                os.remove(part)
            continue
        os.replace(part, part[:-len(".part")])
    spread_datapacks(plan, game_dir)
    return [f"{path}: {error}" for path, error in failed.items()]


def _extract_overrides(plan: PackPlan, game_dir: str):
    """Настройки модов, ресурспаки и прочее из папки overrides архива — в папку игры."""
    if not plan.archive:
        return
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
    """Последний выпуск сборки: {"url", "filename", "version", "id"} (Modrinth .mrpack или CurseForge .zip)."""
    return pack_file(project_id)


def same_version(state: Dict, info: Dict) -> bool:
    """
    Поставлен ли этот выпуск сборки. Точнее всего — по id файла (запоминается при установке). У старых установок
    его нет: у CurseForge версия в manifest («1.24.1») короче названия файла («FTB StoneBlock 4 1.24.1»), поэтому
    ищем её в названии целиком (1.24.1 ≠ 1.24.10).
    """
    if state.get("file_id") and info.get("id"):
        return str(state["file_id"]) == str(info["id"])
    version = str(state.get("version") or "")
    if not version:
        return False
    if version == info.get("version"):
        return True
    pattern = r"(?<![\w.])" + re.escape(version) + r"(?!\w|\.\d)"  # «-1.24.1.zip» — да, «1.24.10» — нет
    return any(re.search(pattern, str(info.get(k) or "")) for k in ("version", "filename"))


def pack_file(project_id: str, state: Optional[Dict] = None) -> Dict:
    """
    Файл выпуска сборки: {"url", "filename", "version", "id"}. state — поставленная сборка: тогда ищется именно
    её выпуск (для проверки и докачки), иначе — последний.
    """
    import curseforge
    from mod_updater_logic import MODRINTH_API_BASE, _get_json
    if curseforge.is_curseforge(project_id):
        files = []
        for index in range(0, 150, 50):  # Свой выпуск может быть не самым новым — смотрим несколько страниц
            page = curseforge._get(f"/mods/{curseforge.mod_id(project_id)}/files",
                                   {"pageSize": 50, "index": index}).get("data") or []
            files += [f for f in page if not f.get("isServerPack")]
            if state is None or len(page) < 50:
                break
        infos = [{"url": f.get("downloadUrl"), "filename": f.get("fileName") or "", "id": f.get("id"),
                  "version": f.get("displayName") or "", "release": f.get("releaseType") == curseforge.RELEASE,
                  "date": _stamp(f.get("fileDate"))} for f in files]
        infos.sort(key=lambda i: (not i["release"], -i["date"]) if state is None else -i["date"])
    else:
        versions = _get_json(f"{MODRINTH_API_BASE}/project/{project_id}/version") or []
        infos = []
        for version in versions:
            primary = next((f for f in version["files"] if f.get("primary")), (version.get("files") or [None])[0])
            if primary:
                infos.append({"url": primary["url"], "filename": primary["filename"], "id": version.get("id"),
                              "version": version.get("version_number") or "",
                              "release": version.get("version_type") == "release"})
        if state is None:
            infos.sort(key=lambda i: not i["release"])  # Список уже от новых к старым: первый релиз
    if state is not None:
        infos = [i for i in infos if same_version(state, i)]
        if not infos:
            raise ValueError(tr("Выпуск {0} этой сборки больше не найден в каталоге.", state.get("version") or "?"))
    if not infos:
        raise ValueError(tr("У сборки нет файлов для скачивания"))
    info = infos[0]
    if not info.get("url"):
        raise ValueError(tr("Автор разрешает скачивать сборку только с сайта CurseForge"))
    return {k: info[k] for k in ("url", "filename", "version", "id")}


def pack_changelog(project_id: str, current_version: str, target_version: str = "", game_version: str = "",
                   limit: int = 8) -> List[tuple]:
    """
    «Что нового»: [(версия, список изменений в Markdown)] от новой к старой — выпуски от target_version до
    current_version (не включая её), для той же версии Minecraft; если новая — релиз, беты пропускаем.
    Ошибки сети — пустой список: окно обновления работает и без него.
    """
    import curseforge
    from mod_updater_logic import MODRINTH_API_BASE, _get_json
    result = []
    try:
        if curseforge.is_curseforge(project_id):
            files = curseforge._get(f"/mods/{curseforge.mod_id(project_id)}/files", {"pageSize": 30}).get("data") or []
            files = sorted((f for f in files if not f.get("isServerPack")), key=lambda f: -_stamp(f.get("fileDate")))
            started = not target_version
            for file in files:
                names = (file.get("displayName") or "", file.get("fileName") or "")
                started = started or target_version in names
                if not started:
                    continue
                if current_version and current_version in names or len(result) >= limit:
                    break
                result.append((names[0] or names[1], curseforge.file_changelog(project_id, file["id"])))
        else:
            started, release_only = not target_version, False
            for version in _get_json(f"{MODRINTH_API_BASE}/project/{project_id}/version"):
                number = version.get("version_number") or ""
                if not started:
                    if number != target_version:
                        continue
                    started, release_only = True, version.get("version_type") == "release"
                if number == current_version or len(result) >= limit:
                    break
                if game_version and game_version not in (version.get("game_versions") or []):
                    continue  # Ветка сборки для другой версии Minecraft
                if release_only and version.get("version_type") != "release":
                    continue
                result.append((number, version.get("changelog") or ""))
    except Exception as e:
        logging.debug(f"Список изменений сборки {project_id} не загружен: {e}")
    return result


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
    previous = load_state(game_dir)
    overrides = {}
    for relative in _override_members(plan):
        if os.path.isfile(_abs(game_dir, relative)):
            overrides[relative] = _sha1(_abs(game_dir, relative))
    state = {"project_id": project_id, "title": title or plan.name, "version": plan.version,
             "game_version": plan.game_version, "loader": plan.loader, "loader_version": plan.loader_version,
             "files": [f.path for f in plan.files], "overrides": overrides,
             "archive": os.path.abspath(plan.archive) if plan.archive else "",
             "file_id": (plan.source_file or {}).get("id")
             or ((previous or {}).get("file_id") if (previous or {}).get("version") == plan.version else None)}
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
        if item.world:  # Карту, поставленную раньше, не трогаем: в ней играют. Новую — добавим
            if item.path not in old_files:
                download.append(item)
            continue
        current = _on_disk(game_dir, item.path)
        if not current or not item.hashes or not _check_hash(_abs(game_dir, current), item.hashes):
            download.append(item)
    remove = sorted(filter(None, (_on_disk(game_dir, p) for p in old_files - new_paths)))
    substitutes = state.get("substitutes") or {}  # Похожие файлы с Modrinth вместо ушедших из сборки — тоже убрать
    remove += sorted(filter(None, (_on_disk(game_dir, substitutes[p]) for p in old_files - new_paths
                                   if p in substitutes)))
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
                 cancelled: Callable[[], bool] = lambda: False, session=None, job=None) -> str:
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
               "added": [], "backed_up": [], "worlds": [], "state": previous}

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
        failed = download_files(jobs, progress, cancelled, session, job=job)
        if failed:
            raise ValueError("\n".join(f"{path}: {error}" for path, error in list(failed.items())[:12]))
        progress(len(update.download), len(update.download), tr("Обновляем файлы сборки…"))
        for item, part in jobs:
            if item.world:
                for world in unpack_world(part, os.path.join(game_dir, "saves"), os.path.basename(item.path)):
                    journal["worlds"].append(os.path.relpath(world, game_dir).replace(os.sep, "/"))
            else:
                with open(part, "rb") as f:
                    put(item.path, f.read())
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
    for relative in journal.get("worlds", []):  # Карты, распакованные этим обновлением
        shutil.rmtree(_abs(game_dir, relative), ignore_errors=True)
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
    if latest.get("version") and not same_version(state, latest):
        return {"state": state, "latest": latest}
    return None


# --- Проверка и докачка поставленной сборки ---
#
# Сборка сверяется со своим выпуском (тем, что поставлен, а не последним): файлы, которых нет на диске, и файлы,
# у которых не совпала контрольная сумма (оборванная или испорченная загрузка), качаются заново. Выключенный мод
# остаётся выключенным. Пропавшие настройки из сборки возвращаются, а изменённые пользователем — не трогаем.
# Миры не проверяются: в них играют, и они меняются.

def installed_plan(state: Dict, folder: str, session=None) -> PackPlan:
    """План поставленного выпуска: из сохранённого архива или скачанный заново из каталога."""
    archive = state.get("archive") or ""
    plan = None
    if archive and os.path.isfile(archive):
        try:
            plan = load_plan(archive)
        except (ValueError, OSError, zipfile.BadZipFile):
            plan = None
        if plan is not None and str(plan.version) != str(state.get("version") or ""):
            plan = None  # Архив от другого выпуска
    if plan is None:
        info = pack_file(state["project_id"], state)
        plan = load_plan(download_pack(info, folder, session))
        plan.source_file = info
    return plan


@dataclass
class RepairPlan:
    missing: List[PackFile]           # Файлов сборки нет на диске
    broken: List[PackFile]            # Есть, но контрольная сумма не совпала
    overrides: List[str]              # Пропавшие настройки из сборки
    manual: List[PackFile]            # Пропали файлы, которые автор разрешает качать только с сайта
    checked: int                      # Сколько файлов проверено
    prism_cfg: List[str] = field(default_factory=list)  # Недостающие поля instance.cfg сборки Prism / MultiMC

    @property
    def download(self) -> List[PackFile]:
        return self.missing + self.broken

    @property
    def empty(self) -> bool:
        return not (self.missing or self.broken or self.overrides or self.manual or self.prism_cfg)


def plan_repair(plan: PackPlan, game_dir: str, progress: Callable[[int, int, str], None] = lambda d, t, s: None,
                cancelled: Callable[[], bool] = lambda: False) -> RepairPlan:
    """Что не так со сборкой: каких файлов нет и какие испорчены (сверка по контрольным суммам)."""
    import manual_files
    files = [f for f in plan.files if not f.world]
    substitutes = manual_files.substitutes_of(game_dir)  # Заменённые похожим файлом с Modrinth — на месте
    missing, broken, manual = [], [], []
    for i, item in enumerate(files):
        if cancelled():
            raise InstallCancelled()
        progress(i, len(files), tr("Проверяем {0}", item.name or os.path.basename(item.path)))
        current = _on_disk(game_dir, item.path)
        if not current and substitutes.get(item.path) and _on_disk(game_dir, substitutes[item.path]):
            continue
        if not current:
            (missing if item.url else manual).append(item)
        elif item.url and item.hashes and not _check_hash(_abs(game_dir, current), item.hashes):
            broken.append(item)
    overrides = [r for r in sorted(_override_members(plan)) if not os.path.isfile(_abs(game_dir, r))]
    progress(len(files), len(files), tr("Проверка закончена"))
    return RepairPlan(missing, broken, overrides, manual, len(files), missing_prism_cfg(game_dir))


def apply_repair(repair: RepairPlan, plan: PackPlan, game_dir: str,
                 progress: Callable[[int, int, str], None] = lambda d, t, s: None,
                 cancelled: Callable[[], bool] = lambda: False, session=None, job=None) -> List[str]:
    """Качает недостающее и испорченное, возвращает пропавшие настройки. Возвращает ошибки (пустой — всё в порядке)."""
    import tempfile

    from mod_updater_logic import SESSION
    errors = []
    if repair.download:
        work = tempfile.mkdtemp(prefix="repair-", dir=os.path.join(game_dir, STATE_DIR) if os.path.isdir(
            os.path.join(game_dir, STATE_DIR)) else None)
        try:
            jobs = [(item, os.path.join(work, f"{i}.part")) for i, item in enumerate(repair.download)]
            failed = download_files(jobs, progress, cancelled, session or SESSION, job=job)
            for item, part in jobs:
                if item.path in failed:
                    errors.append(f"{item.path}: {failed[item.path]}")
                    continue
                relative = _on_disk(game_dir, item.path) or item.path  # Выключенный мод остаётся выключенным
                destination = _abs(game_dir, relative)
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                os.replace(part, destination)
        finally:
            import shutil
            shutil.rmtree(work, ignore_errors=True)
    if repair.overrides:
        progress(len(repair.download), len(repair.download), tr("Возвращаем настройки сборки…"))
        members = _override_members(plan)
        with zipfile.ZipFile(plan.archive) as archive:
            for relative in repair.overrides:
                destination = _abs(game_dir, relative)
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                with archive.open(members[relative]) as source, open(destination, "wb") as out:
                    out.write(source.read())
    if repair.prism_cfg:
        complete_prism_cfg(game_dir)
    picked = pick_manual_downloads(repair.manual, game_dir)
    repair.manual = [m for m in repair.manual if m not in picked]  # Остались — их всё ещё нужно скачать с сайта
    spread_datapacks(plan, game_dir)
    logging.info(tr("Сборка проверена: перекачано {0}, возвращено настроек {1}, ошибок {2}",
                    len(repair.download) - len(errors), len(repair.overrides), len(errors)))
    return errors


def _prism_cfg_path(game_dir: str) -> Optional[str]:
    """instance.cfg сборки Prism / MultiMC по папке игры (None — это не их сборка)."""
    from game_runner import instance_dir
    folder = instance_dir(game_dir)
    if os.path.isfile(os.path.join(folder, "mmc-pack.json")) and os.path.isfile(os.path.join(folder, "instance.cfg")):
        return os.path.join(folder, "instance.cfg")
    return None


def _cfg_keys(text: str) -> set:
    return {line.split("=", 1)[0].strip() for line in text.splitlines() if "=" in line and not line.startswith(";")}


def missing_prism_cfg(game_dir: str) -> List[str]:
    """Каких полей не хватает в instance.cfg (сборки, поставленные старыми версиями программы)."""
    path = _prism_cfg_path(game_dir)
    if not path:
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        have = _cfg_keys(f.read())
    return [key for key, _ in PRISM_CFG_DEFAULTS if key not in have] + (["uuid"] if "uuid" not in have else [])


def complete_prism_cfg(game_dir: str) -> int:
    """
    Дописывает в секцию [General] instance.cfg недостающие поля со значениями по умолчанию. Заданные
    значения не меняются. Перед записью — копия файла. Возвращает, сколько полей добавлено.
    """
    import shutil
    import uuid
    path = _prism_cfg_path(game_dir)
    missing = missing_prism_cfg(game_dir)
    if not path or not missing:
        return 0
    defaults = dict(PRISM_CFG_DEFAULTS)
    defaults["uuid"] = uuid.uuid4().hex
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    added = [f"{key}={defaults[key]}" for key in missing]
    general = next((i for i, line in enumerate(lines) if line.strip().lower() == "[general]"), None)
    if general is None:
        lines = ["[General]"] + added + lines
    else:
        end = next((i for i in range(general + 1, len(lines)) if lines[i].startswith("[")), len(lines))
        while end > general + 1 and not lines[end - 1].strip():
            end -= 1  # Пустые строки перед следующей секцией оставляем после добавленного
        lines[end:end] = added
    shutil.copy2(path, path + ".modupdater.bak")
    with open(path + ".tmp", "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(path + ".tmp", path)
    logging.info(tr("Сборка Prism: в instance.cfg дописано полей — {0}", len(added)))
    return len(added)


def pick_manual_downloads(items: List[PackFile], game_dir: str, folder: Optional[str] = None,
                          since: float = 0.0) -> List[PackFile]:
    """
    Файлы «только с сайта», которые пользователь скачал браузером: ищем их в папке «Загрузки» (по имени или по
    размеру и контрольной сумме) и кладём в сборку копией. Возвращает разложенные.
    """
    import manual_files
    found = manual_files.match_downloads(items, folder or downloads_folder(), since)
    picked = []
    for item in items:
        if item.path in found:
            manual_files.place(item, found[item.path], game_dir)
            picked.append(item)
    if picked:
        logging.info(tr("Сборка: из папки «Загрузки» взято файлов — {0}", len(picked)))
    return picked


def remember_source(game_dir: str, plan: PackPlan):
    """Дописывает к запомненной сборке, из какого архива и выпуска она (у установок старых версий этого нет)."""
    state = load_state(game_dir)
    if not state or str(state.get("version") or "") != str(plan.version):
        return
    state["archive"] = os.path.abspath(plan.archive)
    if (plan.source_file or {}).get("id"):
        state["file_id"] = plan.source_file["id"]
    with open(state_path(game_dir), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
