# mod_updater_logic.py
"""Сетевые запросы к Modrinth и API загрузчиков, скачивание файлов."""
import datetime
import hashlib
import json
import logging
import os
import time
import xml.etree.ElementTree as ET  # type: ignore
from typing import Callable, Dict, Iterable, List, Optional, Union

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from packaging.version import InvalidVersion, parse as parse_version
from i18n import tr

# Константы для API Modrinth
MODRINTH_API_BASE = "https://api.modrinth.com/v2"
# User-Agent важен, чтобы API Modrinth не блокировал ваши запросы
HEADERS = {
    "User-Agent": "TheMrEvgen/MinecraftModUpdater (https://github.com/TheMrEvgen)"
}

DEFAULT_TIMEOUT = 15
DOWNLOAD_CHUNK_SIZE = 128 * 1024
# Размеры пакетов: POST-запросы принимают большие списки, GET ограничен длиной URL
HASH_CHUNK_SIZE = 500
IDS_CHUNK_SIZE = 100


def _create_session() -> requests.Session:
    """
    Сессия с повторными попытками на уровне транспорта: повторяются только временные ошибки
    (429 и 5xx, обрывы соединения). Ошибки клиента (400, 404) не повторяются — это бессмысленно.
    Retry учитывает заголовок Retry-After, который Modrinth присылает при превышении лимита.
    """
    retry = Retry(
        total=3,
        connect=3,
        read=2,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# Сессия переиспользует TCP-соединения между запросами
SESSION = _create_session()

# Простой кэш в памяти для результатов, которые не меняются в течение сессии
SESSION_CACHE: Dict[str, object] = {}


class DownloadError(Exception):
    """Не удалось скачать файл."""


class HashMismatchError(DownloadError):
    """Хэш скачанного файла не совпал с ожидаемым."""


class DownloadCancelled(DownloadError):
    """Скачивание прервано пользователем."""


Loaders = Union[str, List[str], None]


def _loader_list(loader: Loaders) -> List[str]:
    """Загрузчик для запросов: строка ('fabric') или список (у шейдеров — iris, optifine…)."""
    if not loader:
        return []
    return [loader] if isinstance(loader, str) else list(loader)


def _chunks(items: List, size: int) -> Iterable[List]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _get_json(url: str, params=None, timeout: int = DEFAULT_TIMEOUT):
    response = SESSION.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _post_json(url: str, payload, timeout: int = DEFAULT_TIMEOUT):
    response = SESSION.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()


# --- Утилиты ---

def get_file_hash(filepath: str, hash_algo: str = "sha1") -> str:
    """Вычисляет хэш-сумму файла (SHA1 по умолчанию)."""
    h = hashlib.new(hash_algo)
    with open(filepath, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def safe_filename(filename: str) -> str:
    """
    Защита от обхода путей: имя файла из API не должно содержать каталогов.
    Возвращает только базовое имя или выбрасывает ValueError.
    """
    name = os.path.basename((filename or "").replace("\\", "/"))
    if not name or name in (".", "..") or name.startswith("."):
        raise ValueError(tr('Недопустимое имя файла: {0!r}', filename))
    return name


def select_primary_file(version_info: Optional[Dict]) -> Optional[Dict]:
    """Возвращает основной файл версии; если флаг primary нигде не стоит — первый файл."""
    if not version_info:
        return None
    files = version_info.get('files') or []
    return next((f for f in files if f.get('primary')), files[0] if files else None)


def format_date(date_str: Optional[str]) -> str:
    """ISO-дата Modrinth -> 'YYYY-MM-DD'."""
    if not date_str:
        return ""
    try:
        return datetime.datetime.fromisoformat(date_str.replace('Z', '+00:00')).strftime('%Y-%m-%d')
    except ValueError:
        return date_str.split('T')[0]


def _parse_date(date_str: Optional[str]) -> Optional[datetime.datetime]:
    if not date_str:
        return None
    try:
        return datetime.datetime.fromisoformat(date_str.replace('Z', '+00:00'))
    except ValueError:
        return None


def is_newer_version(candidate: Optional[Dict], current: Optional[Dict]) -> bool:
    """
    True, если candidate — действительно более новая версия, чем current.
    Сравнение по id и дате публикации: простое сравнение номеров версий (!=) предлагало
    «обновление» на более старый релиз, если у пользователя стояла свежая бета.
    """
    if not candidate:
        return False
    if not current:
        return True
    if candidate.get('id') and candidate.get('id') == current.get('id'):
        return False
    if candidate.get('version_number') == current.get('version_number'):
        return False
    candidate_date = _parse_date(candidate.get('date_published'))
    current_date = _parse_date(current.get('date_published'))
    if candidate_date and current_date:
        return candidate_date > current_date
    return True


# --- Modrinth API ---

def get_mods_info_from_hashes(hashes: List[str]) -> Dict[str, Dict]:
    """Информация об установленных версиях по SHA1 хэшам (POST /version_files)."""
    if not hashes:
        return {}
    all_data: Dict[str, Dict] = {}
    url = f"{MODRINTH_API_BASE}/version_files"
    for chunk in _chunks(hashes, HASH_CHUNK_SIZE):
        try:
            all_data.update(_post_json(url, {"hashes": chunk, "algorithm": "sha1"}))
        except (requests.RequestException, json.JSONDecodeError) as e:
            logging.error(f"Ошибка при запросе к API Modrinth по хэшам: {e}")
    logging.info(f"Получена информация для {len(all_data)} из {len(hashes)} запрошенных хэшей.")
    return all_data


def get_latest_versions_from_hashes(hashes: List[str], game_version: str, loader: Loaders,
                                    version_types: Optional[List[str]] = None) -> Dict[str, Dict]:
    """
    Последние совместимые версии сразу для всех установленных файлов (POST /version_files/update).
    Один запрос вместо отдельного запроса на каждый мод.
    """
    if not hashes:
        return {}
    url = f"{MODRINTH_API_BASE}/version_files/update"
    result: Dict[str, Dict] = {}
    for chunk in _chunks(hashes, HASH_CHUNK_SIZE):
        payload = {"hashes": chunk, "algorithm": "sha1", "loaders": _loader_list(loader), "game_versions": [game_version]}
        if version_types:
            payload["version_types"] = version_types
        result.update(_post_json(url, payload, timeout=30))
    logging.info(f"Получены последние версии для {len(result)} из {len(hashes)} файлов.")
    return result


def get_versions_by_ids(version_ids: List[str]) -> Dict[str, Dict]:
    """Информация о нескольких версиях по их ID (GET /versions)."""
    result: Dict[str, Dict] = {}
    for chunk in _chunks(list(version_ids), IDS_CHUNK_SIZE):
        for v in _get_json(f"{MODRINTH_API_BASE}/versions", params={"ids": json.dumps(chunk)}):
            result[v['id']] = v
    return result


def get_all_versions(project_id: str, game_version: str, loader: Loaders) -> List[Dict]:
    """Список всех версий мода для указанной версии игры и загрузчика (новые первыми)."""
    url = f"{MODRINTH_API_BASE}/project/{project_id}/version"
    params = {"game_versions": json.dumps([game_version])}
    if _loader_list(loader):
        params["loaders"] = json.dumps(_loader_list(loader))
    logging.debug(f"Поиск версий для project_id={project_id}: game_version={game_version}, loader={loader}")
    return _get_json(url, params=params)


def get_latest_version(project_id: str, game_version: str, loader: Loaders, target_version: Optional[str] = None,
                       version_types: Optional[List[str]] = None) -> Optional[Dict]:
    """Находит версию мода (точную или последнюю в выбранном канале) для версии игры и загрузчика."""
    versions = get_all_versions(project_id, game_version, loader)
    if not versions:
        logging.info(f"Для проекта {project_id} не найдено версий, совместимых с {game_version} и {loader}.")
        return None

    if target_version:
        for v in versions:
            if v['version_number'] == target_version:
                return v
        logging.warning(f"Точная версия {target_version} для {project_id} не найдена. Откат к последней доступной.")

    if version_types:
        filtered = [v for v in versions if v.get('version_type', 'release') in version_types]
        if not filtered:
            logging.info(f"Для проекта {project_id} нет версий в канале {version_types}.")
            return None
        versions = filtered
    # API возвращает версии, отсортированные по дате, первая - самая новая
    return versions[0]


def get_projects_info_from_ids(ids: Iterable[str]) -> Dict[str, Dict]:
    """Информация о нескольких проектах по их ID (название, иконка, client_side/server_side)."""
    ids = list(dict.fromkeys(i for i in ids if i))
    if not ids:
        return {}
    projects_dict: Dict[str, Dict] = {}
    for chunk in _chunks(ids, IDS_CHUNK_SIZE):
        for p in _get_json(f"{MODRINTH_API_BASE}/projects", params={"ids": json.dumps(chunk)}):
            projects_dict[p['id']] = p
    logging.debug(f"Получена информация о {len(projects_dict)} проектах.")
    return projects_dict


SEARCH_SORTS = ("relevance", "downloads", "follows", "updated", "newest")


def search_projects(query: str, project_type: str = "mod", game_version: Optional[str] = None,
                    loaders: Loaders = None, index: str = "relevance", offset: int = 0, limit: int = 20,
                    categories: Optional[List[str]] = None, side: Optional[str] = None) -> Dict:
    """
    Поиск в каталоге Modrinth (GET /search). Возвращает {"hits": [...], "total_hits": N}.
    Фильтры передаются facets: внутри списка — «или», между списками — «и».
    """
    facets = [[f"project_type:{project_type}"]]
    if game_version:
        facets.append([f"versions:{game_version}"])
    loader_list = _loader_list(loaders)
    if loader_list:
        facets.append([f"categories:{loader}" for loader in loader_list])
    for category in categories or []:  # Каждая категория — отдельное условие «и»
        facets.append([f"categories:{category}"])
    if side == "client":
        facets.append(["client_side:required", "client_side:optional"])
    elif side == "server":
        facets.append(["server_side:required", "server_side:optional"])
    params = {"query": query or "", "facets": json.dumps(facets), "index": index if index in SEARCH_SORTS else "relevance",
              "offset": max(0, offset), "limit": max(1, min(limit, 100))}
    return _get_json(f"{MODRINTH_API_BASE}/search", params=params)


def get_project(project_id: str) -> Dict:
    """Полная информация о проекте (GET /project/{id}): описание body, галерея, ссылки."""
    return _get_json(f"{MODRINTH_API_BASE}/project/{project_id}")


def get_categories(project_type: str = "mod") -> List[Dict]:
    """Категории Modrinth для типа проекта (GET /tag/category) — для фильтров каталога."""
    cache_key = "categories"
    if cache_key not in SESSION_CACHE:
        SESSION_CACHE[cache_key] = _get_json(f"{MODRINTH_API_BASE}/tag/category")
    return [c for c in SESSION_CACHE[cache_key] if c.get("project_type") == project_type]  # type: ignore


def get_game_versions(include_snapshots: bool = False) -> List[str]:
    """Версии Minecraft (новые первыми) из справочника Modrinth."""
    cache_key = f"game_versions:{include_snapshots}"
    if cache_key not in SESSION_CACHE:
        data = _get_json(f"{MODRINTH_API_BASE}/tag/game_version")
        data.sort(key=lambda v: v.get("date") or "", reverse=True)
        SESSION_CACHE[cache_key] = [v["version"] for v in data
                                    if include_snapshots or v.get("version_type") == "release"]
    return list(SESSION_CACHE[cache_key])  # type: ignore


# --- Версии загрузчиков ---

def _find_best_game_version(all_game_versions: List[Dict], game_version: str) -> Optional[str]:
    """Самая новая версия игры из API, не превосходящая версию пользователя (учитывает pre-release)."""
    user_v = parse_version(game_version)
    best_match = None
    for v_info in all_game_versions:
        api_v_str = v_info.get('version')
        if not api_v_str:
            continue
        try:
            api_v = parse_version(api_v_str)
        except InvalidVersion:
            continue  # Снапшоты вида 24w14a не являются валидными версиями
        if api_v.release <= user_v.release and (best_match is None or api_v > best_match):
            best_match = api_v
    return str(best_match) if best_match else None


def _get_meta_loader_version(meta_base: str, game_version: str) -> Optional[str]:
    """Общая логика для Fabric и Quilt: у них одинаковая структура meta API."""
    best_match = _find_best_game_version(_get_json(f"{meta_base}/versions/game", timeout=10), game_version)
    if not best_match:
        logging.warning(f"Не удалось найти совместимую версию игры в {meta_base} для {game_version}")
        return None
    if best_match != game_version:
        logging.info(f"Для {game_version} используется версия {best_match} для поиска загрузчика.")

    data = _get_json(f"{meta_base}/versions/loader/{best_match}", timeout=10)

    def is_stable(loader_info: Dict) -> bool:
        # У Fabric есть флаг stable, у Quilt его нет — тогда определяем по номеру версии (beta/rc)
        if 'stable' in loader_info:
            return bool(loader_info['stable'])
        try:
            return not parse_version(loader_info['version']).is_prerelease
        except InvalidVersion:
            return False

    stable = next((v['loader']['version'] for v in data if is_stable(v['loader'])), None)
    return stable or (data[0]['loader']['version'] if data else None)


def neoforge_version_prefix(game_version: str) -> Optional[str]:
    """
    Префикс версии NeoForge для версии игры:
      старый формат: 1.20.4 -> '20.4.', 1.21 -> '21.0.'
      новый формат (с 2026): 26.3 -> '26.3.0.', 26.3.1 -> '26.3.1.'
    Завершающая точка обязательна: иначе '21.1' совпадает с '21.10.x' и '21.11.x'.
    """
    parts = game_version.split('.')
    if len(parts) < 2 or not all(p.isdigit() for p in parts[:3]):
        return None
    if parts[0] == "1":
        mc_minor = parts[2] if len(parts) > 2 else "0"
        return f"{parts[1]}.{mc_minor}."
    patch = parts[2] if len(parts) > 2 else "0"
    return f"{parts[0]}.{parts[1]}.{patch}."


def pick_neoforge_version(all_versions: List[str], game_version: str) -> Optional[str]:
    """Выбирает последнюю стабильную (или любую последнюю) версию NeoForge для версии игры."""
    prefix = neoforge_version_prefix(game_version)
    if not prefix:
        logging.warning(f"Некорректная версия игры для NeoForge: {game_version}")
        return None

    candidates = []
    for v_str in all_versions:
        if v_str and v_str.startswith(prefix):
            try:
                candidates.append((parse_version(v_str), v_str))
            except InvalidVersion:
                continue
    if not candidates:
        logging.warning(f"Не найдено подходящих версий NeoForge для префикса {prefix}")
        return None
    candidates.sort(reverse=True)
    stable = next((s for v, s in candidates if not v.is_prerelease), None)
    return stable or candidates[0][1]


def get_latest_loader_version(loader: str, game_version: str) -> Optional[str]:
    """Последняя стабильная версия загрузчика для версии игры."""
    cache_key = f"loader_version:{loader}:{game_version}"
    if cache_key in SESSION_CACHE:
        return SESSION_CACHE[cache_key]  # type: ignore

    logging.info(f"Запрос последней версии загрузчика: {loader} для Minecraft {game_version}")
    result = None
    try:
        if loader == "fabric":
            result = _get_meta_loader_version("https://meta.fabricmc.net/v2", game_version)
        elif loader == "quilt":
            result = _get_meta_loader_version("https://meta.quiltmc.org/v3", game_version)
        elif loader == "forge":
            data = _get_json("https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json", timeout=10)
            result = data.get("promos", {}).get(f"{game_version}-latest")
        elif loader == "neoforge":
            # У NeoForge нет promotions_slim.json, разбираем maven-metadata
            response = SESSION.get("https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml", timeout=10)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            result = pick_neoforge_version([v.text for v in root.findall('.//version') if v.text], game_version)
    except requests.RequestException as e:
        logging.error(f"Сетевая ошибка при получении версии загрузчика {loader}: {e}")
    except (json.JSONDecodeError, KeyError, ET.ParseError, InvalidVersion) as e:
        logging.error(f"Ошибка разбора ответа для загрузчика {loader}: {e}")

    SESSION_CACHE[cache_key] = result
    if not result:
        logging.warning(f"Не удалось найти последнюю версию для загрузчика {loader}.")
    return result


# --- Скачивание ---

def verify_file_hashes(filepath: str, expected_hashes: Optional[Dict[str, str]]) -> bool:
    """Проверяет файл по самому надёжному из доступных хэшей (sha512, затем sha1)."""
    if not expected_hashes:
        return True
    for algo in ("sha512", "sha1"):
        expected = expected_hashes.get(algo)
        if expected:
            return get_file_hash(filepath, algo) == expected.lower()
    return True


def download_file(url: str, dest_path: str, expected_hashes: Optional[Dict[str, str]] = None,
                  progress_callback: Optional[Callable[[int, int], None]] = None,
                  is_cancelled: Optional[Callable[[], bool]] = None,
                  retries: int = 3, delay: int = 2) -> None:
    """
    Скачивает файл в dest_path с проверкой хэша. При ошибке выбрасывает DownloadError,
    HashMismatchError или DownloadCancelled; частично скачанный файл удаляется.
    """
    filename = os.path.basename(dest_path)
    last_error: Optional[Exception] = None

    for attempt in range(retries):
        try:
            logging.info(f"Скачивание {filename} (попытка {attempt + 1}/{retries})...")
            with SESSION.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                total_size = int(r.headers.get('content-length', 0))
                downloaded_size = 0
                with open(dest_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                        if is_cancelled and is_cancelled():
                            raise DownloadCancelled(filename)
                        if chunk:
                            f.write(chunk)
                            downloaded_size += len(chunk)
                            if progress_callback and total_size > 0:
                                progress_callback(downloaded_size, total_size)

            if os.path.getsize(dest_path) == 0:
                raise DownloadError(tr("Скачанный файл пуст"))
            if not verify_file_hashes(dest_path, expected_hashes):
                raise HashMismatchError(tr('Хэш файла {0} не совпадает с ожидаемым', filename))
            logging.info(f"Файл {filename} успешно скачан и проверен ({downloaded_size} байт).")
            return

        except DownloadCancelled:
            _remove_quietly(dest_path)
            raise
        except (requests.RequestException, OSError, DownloadError) as e:
            last_error = e
            logging.warning(f"Ошибка скачивания {filename} на попытке {attempt + 1}: {e}")
            _remove_quietly(dest_path)
            if attempt < retries - 1:
                time.sleep(delay * (2 ** attempt))

    logging.error(f"Не удалось скачать {filename} после {retries} попыток.")
    if isinstance(last_error, HashMismatchError):
        raise last_error
    raise DownloadError(str(last_error))


def download_bytes(url: str, timeout: int = 15, max_size: int = 2 * 1024 * 1024) -> bytes:
    """Скачивает небольшой файл (например, иконку) в память с ограничением размера."""
    with SESSION.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        data = bytearray()
        for chunk in r.iter_content(chunk_size=64 * 1024):
            data.extend(chunk)
            if len(data) > max_size:
                raise DownloadError(tr("Файл слишком большой"))
        return bytes(data)


def _remove_quietly(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as e:
        logging.error(f"Не удалось удалить файл {path}: {e}")
