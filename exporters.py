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
