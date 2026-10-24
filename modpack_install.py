"""
Установка готовой сборки из каталога (без Qt): .mrpack (Modrinth) или .zip с manifest.json (CurseForge).

    plan = load_plan(путь к файлу сборки)          — что внутри: версия игры, загрузчик, файлы, overrides
    target = prism_target(...) | folder_target(...) — куда ставить: новая сборка Prism или папка
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


def install(plan: PackPlan, target: Target, progress: ProgressCallback = lambda d, t, s: None,
            cancelled: Callable[[], bool] = lambda: False, session=None) -> List[str]:
    """Ставит сборку. Возвращает ошибки (пустой список — всё скачано). Файлы «только с сайта» пропускаются."""
    from mod_updater_logic import SESSION
    session = session or SESSION
    os.makedirs(target.game_dir, exist_ok=True)
    if target.instance_dir:
        write_prism_instance(target, plan)
    errors = []
    files = [f for f in plan.files if f.url]
    for i, item in enumerate(files):
        if cancelled():
            raise InstallCancelled()
        progress(i, len(files), item.name or os.path.basename(item.path))
        destination = os.path.join(target.game_dir, *item.path.split("/"))
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        try:
            response = session.get(item.url, timeout=60)
            response.raise_for_status()
            with open(destination + ".part", "wb") as f:
                f.write(response.content)
            if not _check_hash(destination + ".part", item.hashes):
                os.remove(destination + ".part")
                errors.append(tr("{0}: контрольная сумма не совпала", item.path))
                continue
            os.replace(destination + ".part", destination)
        except Exception as e:  # Один файл не скачался — остальные ставим
            errors.append(f"{item.path}: {e}")
            logging.warning(tr("Сборка: не удалось скачать {0}: {1}", item.path, e))
    progress(len(files), len(files), tr("Распаковываем настройки сборки…"))
    _extract_overrides(plan, target.game_dir)
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
