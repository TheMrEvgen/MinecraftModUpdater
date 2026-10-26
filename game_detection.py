"""Определение установленных версий Minecraft и загрузчиков по папке versions."""
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional
from i18n import tr

SUPPORTED_LOADERS = ("fabric", "neoforge", "forge", "quilt")

# Версии игры: старый формат 1.x.y и новый «годовой» формат 26.x / 26.x.y (с 2026 года)
GAME_VERSION_RE = r"(?:1\.\d+(?:\.\d+)?|[2-9]\d\.\d+(?:\.\d+)?)"

_LOADER_VERSION_PATTERNS = {
    'fabric': r'fabric-loader-([\d\.\w-]+)-',
    'quilt': r'quilt-loader-([\d\.\w-]+)-',
    'forge': r'forge(?:\d[\d.]*)?-([\d\.]+)',  # 1.20.1-forge-47.2.0 и старое 1.8.9-forge1.8.9-11.15.1.2318
    'neoforge': r'neoforge-([\d\.]+)',
}


@dataclass
class DetectionResult:
    versions: set = field(default_factory=set)        # {(game_version, loader)}
    best_guess: Optional[tuple] = None                # (game_version, loader) самой свежей установки
    best_guess_id: Optional[str] = None               # id из json лучшей установки

    @property
    def game_versions(self) -> list[str]:
        return sorted({v[0] for v in self.versions}, key=_version_sort_key, reverse=True)

    @property
    def loaders(self) -> list[str]:
        return sorted({v[1] for v in self.versions})


def _version_sort_key(version: str):
    return tuple(int(p) if p.isdigit() else 0 for p in version.split('.'))


def extract_game_version(data: dict) -> Optional[str]:
    """Извлекает версию игры из json-файла версии лаунчера."""
    # Способ 1: зависимости - самый надежный метод
    for lib in data.get('libraries', []) or []:
        lib_name = lib.get('name', '') if isinstance(lib, dict) else ''
        if lib_name.startswith(('net.minecraft:client:', 'com.mojang:minecraft:')):
            parts = lib_name.split(':')
            if len(parts) == 3 and re.fullmatch(GAME_VERSION_RE + r"(-\w+)?", parts[2]):
                return parts[2].split('-')[0]

    # Способ 2 и 3: 'inheritsFrom' (Fabric и др.) и 'jar' (старый Forge)
    for key in ('inheritsFrom', 'jar'):
        value = data.get(key)
        if isinstance(value, str):
            match = re.search(GAME_VERSION_RE, value)
            if match:
                return match.group(0)

    # Способ 4: последняя версия в 'id' (e.g., fabric-loader-0.15.11-1.20.1)
    all_versions = re.findall(r"(?<![\d.])" + GAME_VERSION_RE, str(data.get('id', '')))
    return all_versions[-1] if all_versions else None


def extract_loader(version_id: str) -> Optional[str]:
    """Определяет загрузчик по id версии. Без букв вокруг: 'forge' не путается с 'neoforge', а 'forge1.8.9' — Forge."""
    version_id_lower = version_id.lower()
    for loader in SUPPORTED_LOADERS:
        if re.search(r'(?<![a-z])' + re.escape(loader) + r'(?![a-z])', version_id_lower):
            return loader
    return None


def extract_loader_version(loader: str, version_id: str) -> Optional[str]:
    """Извлекает версию загрузчика из id версии лаунчера."""
    pattern = _LOADER_VERSION_PATTERNS.get(loader)
    if not pattern or not version_id:
        return None
    match = re.search(pattern, version_id, re.IGNORECASE)
    return match.group(1) if match else None


def detect_game_versions(game_dir: str) -> DetectionResult:
    """Анализирует json-файлы в <game_dir>/versions и возвращает найденные пары (версия, загрузчик)."""
    result = DetectionResult()
    versions_path = os.path.join(game_dir, 'versions')
    if not os.path.isdir(versions_path):
        logging.warning(tr('Папка версий не найдена по пути: {0}.', versions_path))
        return result

    latest_mod_time = 0.0
    # Лаунчер кладет json на один уровень вложенности: versions/<id>/<id>.json
    for entry in os.scandir(versions_path):
        if not entry.is_dir():
            continue
        for file_entry in os.scandir(entry.path):
            if not file_entry.name.endswith('.json') or not file_entry.is_file():
                continue
            try:
                with open(file_entry.path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
                logging.warning(tr('Не удалось прочитать или разобрать файл версии {0}: {1}', file_entry.path, e))
                continue
            if not isinstance(data, dict):
                continue

            version_id = str(data.get('id', ''))
            game_version = extract_game_version(data)
            loader = extract_loader(version_id)
            if not (game_version and loader):
                continue

            logging.debug(f"Версия {file_entry.name}: ({game_version}, {loader}).")
            result.versions.add((game_version, loader))
            mod_time = entry.stat().st_mtime
            if mod_time > latest_mod_time:
                latest_mod_time = mod_time
                result.best_guess = (game_version, loader)
                result.best_guess_id = version_id

    return result
