"""Форматы обмена сборками: строка «Поделиться», JSON для сервера и .mrpack (без зависимостей от Qt)."""
import base64
import json
import zipfile
import zlib
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Union

from mod_updater_logic import select_primary_file
from i18n import tr

SHARE_FORMAT_VERSION = 2

# Значения client_side/server_side проекта Modrinth -> значения env формата .mrpack
_MRPACK_ENV_VALUES = {"required", "optional", "unsupported"}


def iter_installed_entries(mods_data: List[Dict]) -> Iterator[Dict]:
    """Все установленные моды из списка, включая установленные зависимости, без повторов по project_id."""
    seen = set()
    for mod in mods_data:
        candidates = [mod] + [d for d in mod.get('dependencies', []) if d.get('installed') or d.get('filepath')]
        for entry in candidates:
            pid = entry.get('project_id')
            if not pid or pid in seen or not entry.get('filepath'):
                continue
            seen.add(pid)
            yield entry


# --- Строка «Поделиться» и JSON для сервера ---

def build_share_data(mods_data: List[Dict], game_version: str, loader: str, strict: bool) -> Dict:
    mods = {entry['project_id']: entry.get('current_version', '') for entry in iter_installed_entries(mods_data)}
    return {"v": SHARE_FORMAT_VERSION, "mc": game_version, "l": loader, "strict": strict, "mods": mods}


def encode_share_string(share_data: Dict) -> str:
    json_string = json.dumps(share_data, separators=(',', ':'))
    return base64.urlsafe_b64encode(zlib.compress(json_string.encode('utf-8'), level=9)).decode('ascii')


def decode_share_string(share_string: str) -> Dict:
    """Декодирует строку обмена. Выбрасывает ValueError при некорректных данных."""
    try:
        decoded = base64.urlsafe_b64decode(share_string.strip())
        return json.loads(zlib.decompress(decoded).decode('utf-8'))
    except (ValueError, TypeError, zlib.error, UnicodeDecodeError) as e:
        raise ValueError(tr('Некорректная строка: {0}', e)) from e


@dataclass
class ImportRequest:
    version: int
    game_version: str
    loader: str
    target_mods: Union[Dict[str, str], List[str]]
    strict: bool

    @property
    def project_ids(self) -> List[str]:
        return list(self.target_mods.keys()) if isinstance(self.target_mods, dict) else list(self.target_mods)


def parse_share_data(share_data) -> ImportRequest:
    """Проверяет и разбирает данные сборки (форматы v1 и v2). Выбрасывает ValueError."""
    if not isinstance(share_data, dict) or not {'v', 'mc', 'l'} <= share_data.keys():
        raise ValueError(tr("Неверный формат данных."))
    version = share_data['v']
    if version == 1:
        target_mods = share_data.get('p', [])
        if not isinstance(target_mods, list):
            raise ValueError(tr("Неверный формат списка модов."))
        strict = False
    elif version == 2:
        target_mods = share_data.get('mods', {})
        if not isinstance(target_mods, dict):
            raise ValueError(tr("Неверный формат списка модов."))
        strict = bool(share_data.get('strict', False))
    else:
        raise ValueError(tr("Неподдерживаемая версия формата данных."))
    return ImportRequest(version, str(share_data['mc']), str(share_data['l']), target_mods, strict)


def save_server_json(path: str, share_data: Dict) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(share_data, f, indent=4, ensure_ascii=False)


# --- .mrpack ---

def mrpack_env(entry: Dict) -> Dict[str, str]:
    """env файла в .mrpack по client_side/server_side проекта; неизвестные значения -> required."""
    def norm(value: Optional[str]) -> str:
        return value if value in _MRPACK_ENV_VALUES else "required"
    return {"client": norm(entry.get('client_side')), "server": norm(entry.get('server_side'))}


def mrpack_loader_key(loader: str) -> str:
    return f"{loader}-loader" if loader in ('fabric', 'quilt') else loader


# Откуда .mrpack разрешает скачивать файлы (спецификация Modrinth); CurseForge сюда не входит
MRPACK_HOSTS = ("cdn.modrinth.com", "github.com", "raw.githubusercontent.com", "gitlab.com")


def mrpack_allowed(url: Optional[str]) -> bool:
    from urllib.parse import urlparse
    return bool(url) and urlparse(url).hostname in MRPACK_HOSTS


def build_mrpack_index(mods_data: List[Dict], game_version: str, loader: str, loader_version: str,
                       name: Optional[str] = None, skipped: Optional[List[str]] = None) -> Dict:
    """skipped — сюда попадают названия модов, которые формат .mrpack не позволяет сослать (например, с CurseForge)."""
    files = []
    for entry in iter_installed_entries(mods_data):
        primary_file = select_primary_file(entry.get('installed_info'))
        if not primary_file:
            continue
        if not mrpack_allowed(primary_file.get('url')):
            if skipped is not None:
                skipped.append(entry.get('name') or primary_file.get('filename') or "?")
            continue
        hashes = primary_file.get('hashes', {})
        files.append({
            "path": f"mods/{primary_file['filename']}",
            "hashes": {"sha1": hashes.get('sha1', ''), "sha512": hashes.get('sha512', '')},
            "env": mrpack_env(entry),
            "downloads": [primary_file['url']],
            "fileSize": primary_file.get('size', 0),
        })
    pack_name = name or f"Modpack {game_version}"
    return {
        "formatVersion": 1,
        "game": "minecraft",
        "versionId": pack_name,
        "name": pack_name,
        "dependencies": {"minecraft": game_version, mrpack_loader_key(loader): loader_version},
        "files": files,
    }


def write_mrpack(path: str, index: Dict) -> None:
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("modrinth.index.json", json.dumps(index, indent=4, ensure_ascii=False))


# --- Сборка CurseForge (.zip с manifest.json) ---
#
# Моды с CurseForge записываются ссылкой (проект и файл) — CurseForge App скачает их сам. Остальные файлы
# (моды не с CurseForge, настройки, миры…) кладутся в папку overrides архива: при импорте приложение
# распаковывает её в папку игры. Такой архив импортируется в CurseForge App: «Создать профиль» → «Импорт».

CURSEFORGE_OVERRIDES = "overrides"


def curseforge_loader_id(loader: str, game_version: str, loader_version: str) -> str:
    """«forge-47.2.0», «neoforge-21.1.77», «fabric-0.16.5» — как загрузчик записан в manifest.json."""
    version = loader_version or ""
    if loader == "forge" and version.startswith(f"{game_version}-"):
        version = version[len(game_version) + 1:]
    return f"{loader}-{version}"


def build_curseforge_manifest(name: str, version: str, game_version: str, loader: str, loader_version: str,
                              files: List[tuple], author: str = "") -> Dict:
    """files — [(id проекта, id файла)] модов с CurseForge."""
    return {
        "minecraft": {"version": game_version,
                      "modLoaders": [{"id": curseforge_loader_id(loader, game_version, loader_version), "primary": True}]
                      if loader else []},
        "manifestType": "minecraftModpack", "manifestVersion": 1,
        "name": name, "version": version or "1.0", "author": author,
        "files": [{"projectID": int(p), "fileID": int(f), "required": True} for p, f in files],
        "overrides": CURSEFORGE_OVERRIDES,
    }


def write_curseforge_pack(path: str, manifest: Dict, overrides: List[tuple],
                          progress=lambda done, total: None) -> int:
    """
    Пишет .zip сборки CurseForge. overrides — [(файл или папка на диске, путь в папке игры)]; папки — целиком.
    Возвращает число файлов в overrides. Пишется во временный файл и подменяется в конце.
    """
    import os
    items = []
    for source, relative in overrides:
        if os.path.isdir(source):
            for root, _, names in os.walk(source):
                for name in names:
                    full = os.path.join(root, name)
                    items.append((full, relative.rstrip("/") + "/" + os.path.relpath(full, source).replace(os.sep, "/")))
        elif os.path.isfile(source):
            items.append((source, relative))
    tmp = path + ".part"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        for i, (source, relative) in enumerate(items):
            zf.write(source, f"{CURSEFORGE_OVERRIDES}/{relative}")
            progress(i + 1, len(items))
    os.replace(tmp, path)
    return len(items)


CURSEFORGE_EXPORT_FOLDERS = ("config", "defaultconfigs", "kubejs")  # Настройки сборки, которые кладём в архив


def build_curseforge_export(path: str, entries: List[Dict], game_dir: str, name: str, game_version: str,
                            loader: str, loader_version: str = "") -> tuple:
    """
    Экспорт сборки в формат CurseForge. Моды, опознанные на CurseForge (и уже как моды CurseForge, и по
    отпечатку файла), записываются ссылкой; остальные кладутся в архив целиком. Возвращает (по ссылке, в архиве).
    """
    import os

    import curseforge
    from content import enabled_name
    from mod_updater_logic import get_latest_loader_version
    if loader and not loader_version:
        loader_version = get_latest_loader_version(loader, game_version) or ""
    refs, unknown, seen = {}, [], set()
    for entry in entries:
        filepath = entry.get("filepath")
        if not filepath or os.path.normcase(filepath) in seen or not os.path.isfile(filepath):
            continue
        seen.add(os.path.normcase(filepath))
        info = entry.get("installed_info") or {}
        if curseforge.is_curseforge(entry.get("project_id")) and info.get("cf_file_id"):
            refs[curseforge.mod_id(entry["project_id"])] = info["cf_file_id"]
        else:
            unknown.append(filepath)
    if unknown and curseforge.available():  # Мод с Modrinth часто есть и на CurseForge — ищем по отпечатку
        try:
            prints = {curseforge.file_fingerprint(p): p for p in unknown}
            for fp, match in curseforge.match_fingerprints(prints).items():
                file = match.get("file") or {}
                if match.get("mod_id") and file.get("id") and fp in prints:
                    refs[int(match["mod_id"])] = int(file["id"])
                    unknown.remove(prints.pop(fp))
        except Exception as e:  # Без сети всё просто окажется внутри архива
            import logging
            logging.warning(tr("CurseForge: не удалось опознать моды по отпечатку: {0}", e))
    overrides = [(p, "mods/" + enabled_name(os.path.basename(p))) for p in unknown]
    overrides += [(os.path.join(game_dir, n), n) for n in CURSEFORGE_EXPORT_FOLDERS
                  if game_dir and os.path.isdir(os.path.join(game_dir, n))]
    manifest = build_curseforge_manifest(name, "1.0", game_version, loader, loader_version, sorted(refs.items()))
    write_curseforge_pack(path, manifest, overrides)
    return len(refs), len(unknown)
