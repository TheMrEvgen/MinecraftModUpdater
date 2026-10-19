# mod_updater_logic.py
import hashlib
import logging
import os
import requests
import shutil
import json
import time
import xml.etree.ElementTree as ET # type: ignore
from typing import List, Dict, Optional
from packaging.version import parse as parse_version

# Константы для API Modrinth
MODRINTH_API_BASE = "https://api.modrinth.com/v2"
# User-Agent важен, чтобы API Modrinth не блокировал ваши запросы
HEADERS = {
    "User-Agent": "TheMrEvgen/MinecraftModUpdater (https://github.com/TheMrEvgen)"
}

# Создаем сессию для переиспользования TCP-соединений и улучшения стабильности
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Простой кэш в памяти для хранения результатов API-запросов на время сессии
# Ключ - строка запроса, значение - результат.
SESSION_CACHE = {}

def with_retries(retries: int = 3, delay: int = 2, backoff: int = 2):
    """
    Декоратор для повторных попыток выполнения сетевых запросов с экспоненциальной задержкой.
    В случае окончательной неудачи, пробрасывает исключение.
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(retries):
                try:
                    return func(*args, **kwargs)
                except requests.RequestException as e:
                    last_exception = e
                    current_delay = delay * (backoff ** attempt)
                    func_name = getattr(func, '__name__', 'decorated_function')
                    logging.warning(f"Сетевая ошибка в {func_name} (попытка {attempt + 1}/{retries}): {e}. Повтор через {current_delay} сек...")
                    time.sleep(current_delay)
            
            logging.error(f"Функция {func.__name__} окончательно провалилась после {retries} попыток.")
            raise last_exception # type: ignore
        return wrapper
    return decorator

def get_file_hash(filepath: str, hash_algo="sha1") -> str:
    """Вычисляет хэш-сумму файла (SHA1 по умолчанию)."""
    h = hashlib.new(hash_algo)
    with open(filepath, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

def get_mods_info_from_hashes(hashes: List[str]) -> Dict:
    """Получает информацию о модах от API Modrinth по списку SHA1 хэшей, разбивая на чанки."""
    if not hashes:
        return {}

    all_data: Dict[str, Dict] = {}
    CHUNK_SIZE = 100  # Безопасный размер чанка для API Modrinth

    logging.info(f"Запрос информации для {len(hashes)} хэшей (будет разделен на чанки по {CHUNK_SIZE}).")

    for i in range(0, len(hashes), CHUNK_SIZE):
        if not hashes[i:i + CHUNK_SIZE]: continue # Пропускаем пустые чанки
        chunk = hashes[i:i + CHUNK_SIZE]
        url = f"{MODRINTH_API_BASE}/version_files"
        params = {"hashes": chunk, "algorithm": "sha1"}
        
        # Ручная реализация логики повторных попыток для чанков
        for attempt in range(3):
            try:
                logging.debug(f"Запрос информации для чанка из {len(chunk)} хэшей (попытка {attempt + 1}).")
                response = SESSION.post(url, json=params, timeout=15)
                response.raise_for_status()
                data = response.json()
                all_data.update(data)
                logging.debug(f"Ответ для чанка получен, количество записей: {len(data)}.")
                if len(hashes) > CHUNK_SIZE: time.sleep(0.2)
                break # Успех, выходим из цикла попыток
            except requests.RequestException as e:
                logging.warning(f"Ошибка при запросе к API Modrinth по хэшам (чанк {i//CHUNK_SIZE + 1}, попытка {attempt + 1}): {e}")
                if attempt < 2:
                    time.sleep(2 * (2 ** attempt))
                else:
                    logging.error(f"Окончательная ошибка для чанка {i//CHUNK_SIZE + 1} после 3 попыток.")
            except json.JSONDecodeError as e:
                logging.error(f"Ошибка декодирования JSON ответа для чанка {i//CHUNK_SIZE + 1}: {e}")
                break # Нет смысла повторять, если JSON невалидный

    logging.info(f"Суммарно получена информация для {len(all_data)} из {len(hashes)} запрошенных хэшей.")
    return all_data

@with_retries()
def get_latest_version(project_id: str, game_version: str, loader: str, target_version: Optional[str] = None) -> Optional[Dict]:
    """Находит версию мода (точную или последнюю) для указанной версии игры и загрузчика."""
    url = f"{MODRINTH_API_BASE}/project/{project_id}/version"
    params = {"loaders": json.dumps([loader]), "game_versions": json.dumps([game_version])}
    logging.debug(f"Поиск версии (target={target_version}) для project_id={project_id} с параметрами: game_version={game_version}, loader={loader}")
    
    response = SESSION.get(url, params=params, timeout=15)
    response.raise_for_status()
    versions = response.json()
    
    if versions:
        if target_version:
            for v in versions:
                if v['version_number'] == target_version:
                    logging.debug(f"Найдена запрошенная точная версия: {target_version}")
                    return v
            logging.warning(f"Точная версия {target_version} для {project_id} не найдена. Откат к последней доступной.")
        # API возвращает версии, отсортированные по дате, первая - самая новая
        logging.debug(f"Найдено {len(versions)} версий для проекта {project_id}. Выбираем самую новую: {versions[0]['version_number']}")
        return versions[0]
    
    logging.info(f"Для проекта {project_id} не найдено версий, совместимых с {game_version} и {loader}.")
    return None

@with_retries()
def get_all_versions(project_id: str, game_version: str, loader: str) -> List[Dict]:
    """Получает список всех версий мода для указанной версии игры и загрузчика."""
    url = f"{MODRINTH_API_BASE}/project/{project_id}/version"
    params = {"loaders": json.dumps([loader]), "game_versions": json.dumps([game_version])}
    logging.debug(f"Поиск всех версий для project_id={project_id} с параметрами: game_version={game_version}, loader={loader}")
    response = SESSION.get(url, params=params, timeout=15)
    response.raise_for_status()
    return response.json()

@with_retries()
def get_projects_info_from_ids(ids: List[str]) -> Dict[str, Dict]:
    """Получает информацию о нескольких проектах по их ID."""
    if not ids:
        return {}
    
    url = f"{MODRINTH_API_BASE}/projects"
    params = {"ids": json.dumps(ids)}
    
    logging.info(f"Запрос информации для {len(ids)} проектов.")
    response = SESSION.get(url, params=params, timeout=15)
    response.raise_for_status()
    projects = response.json()
    projects_dict = {p['id']: p for p in projects}
    logging.debug(f"Получена информация о {len(projects_dict)} проектах.")
    return projects_dict

def get_latest_loader_version(loader: str, game_version: str) -> Optional[str]:
    """Fetches the latest stable loader version for a given game version."""
    cache_key = f"loader_version:{loader}:{game_version}"
    if cache_key in SESSION_CACHE:
        logging.debug(f"Используем кэшированную версию загрузчика для {loader} {game_version}.")
        return SESSION_CACHE[cache_key]

    logging.info(f"Запрос последней версии загрузчика: {loader} для Minecraft {game_version}")
    result = None
    
    try:
        if loader == "fabric":
            # Find the best matching game version from the API to get a compatible loader
            game_versions_url = "https://meta.fabricmc.net/v2/versions/game" # No retry for this, it's a static list
            response = SESSION.get(game_versions_url, timeout=10)
            response.raise_for_status()
            all_game_versions = response.json()

            user_v = parse_version(game_version)
            best_match_v = None

            # Find the latest game version in the API that is less than or equal to the user's version
            for v_info in all_game_versions:
                api_v_str = v_info.get('version')
                if not api_v_str: continue

                try:
                    api_v = parse_version(api_v_str)
                    if api_v.release <= user_v.release: # Compare releases to handle versions like 1.19.4 and 1.19.4-pre1
                        if best_match_v is None or api_v > best_match_v:
                            best_match_v = api_v
                except Exception:
                    continue # Ignore invalid version strings from API

            if not best_match_v:
                logging.warning(f"Не удалось найти совместимую версию игры в Fabric API для {game_version}")
                return None

            best_match_v_str = str(best_match_v)
            if best_match_v_str != game_version:
                logging.info(f"Для {game_version} используется API-ендпоинт версии {best_match_v_str} для поиска загрузчика.")

            url = f"https://meta.fabricmc.net/v2/versions/loader/{best_match_v_str}" # No retry for this either
            response = SESSION.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
            # Find the first stable version
            for v in data:
                if v['loader']['stable']:                    
                    result = v['loader']['version']
                    break
            if not result and data: result = data[0]['loader']['version'] # Fallback to latest if no stable

        elif loader == "quilt":
            # Quilt has a similar structure
            game_versions_url = "https://meta.quiltmc.org/v3/versions/game" # No retry
            response = SESSION.get(game_versions_url, timeout=10)
            response.raise_for_status()
            all_game_versions = response.json()

            user_v = parse_version(game_version)
            best_match_v = None

            for v_info in all_game_versions:
                api_v_str = v_info.get('version')
                if not api_v_str: continue

                try:
                    api_v = parse_version(api_v_str)
                    if api_v.release <= user_v.release:
                        if best_match_v is None or api_v > best_match_v:
                            best_match_v = api_v
                except Exception:
                    continue

            if not best_match_v:
                logging.warning(f"Не удалось найти совместимую версию игры в Quilt API для {game_version}")
                return None

            best_match_v_str = str(best_match_v)
            if best_match_v_str != game_version:
                logging.info(f"Для {game_version} используется API-ендпоинт версии {best_match_v_str} для поиска загрузчика.")

            url = f"https://meta.quiltmc.org/v3/versions/loader/{best_match_v_str}" # No retry
            response = SESSION.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
            for v in data:
                if v['loader']['stable']:                    
                    result = v['loader']['version']
                    break
            if not result and data: result = data[0]['loader']['version']

        elif loader == "forge":
            url = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json" # No retry
            response = SESSION.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
            promo_key = f"{game_version}-latest"
            result = data.get("promos", {}).get(promo_key)

        elif loader == "neoforge":
            # NeoForge does not have a promotions_slim.json. We must parse the maven metadata.
            try:
                url = "https://maven.neoforged.net/net/neoforged/neoforge/maven-metadata.xml" # No retry
                response = SESSION.get(url, timeout=10)
                response.raise_for_status()
                
                root = ET.fromstring(response.content)
                all_versions_str = [v.text for v in root.findall('.//version')]

                # Determine the prefix for the NeoForge version based on the game version
                # e.g., 1.20.4 -> "20.4", 1.21 -> "21.0"
                parts = game_version.split('.')
                if len(parts) < 2:
                    logging.warning(f"Некорректная версия игры для NeoForge: {game_version}")
                    result = None
                
                mc_major = parts[1]
                mc_minor = parts[2] if len(parts) > 2 else "0"
                neoforge_prefix = f"{mc_major}.{mc_minor}"

                logging.debug(f"Ищем версии NeoForge с префиксом '{neoforge_prefix}' для Minecraft {game_version}")

                candidate_versions = []
                for v_str in all_versions_str:
                    if v_str and v_str.startswith(neoforge_prefix):
                        try:
                            candidate_versions.append(parse_version(v_str))
                        except Exception:
                            continue # Ignore invalid version strings
                
                if not candidate_versions:
                    logging.warning(f"Не найдено подходящих версий NeoForge для префикса {neoforge_prefix}")
                    result = None

                else:
                    candidate_versions.sort(reverse=True)

                    latest_stable = next((v for v in candidate_versions if not v.is_prerelease), None)
                    
                    if latest_stable:
                        result = str(latest_stable)
                    else:
                        result = str(candidate_versions[0])
            except (requests.RequestException, ET.ParseError) as e:
                logging.error(f"Ошибка при получении или парсинге метаданных NeoForge: {e}")
                result = None

    except (requests.RequestException, json.JSONDecodeError, KeyError) as e:
        logging.error(f"Сетевая ошибка при получении версии загрузчика {loader}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logging.error(f"  Ответ сервера ({e.response.status_code}): {e.response.text}")
        result = None
    except (json.JSONDecodeError, KeyError) as e:
        logging.error(f"Ошибка парсинга ответа для загрузчика {loader}: {e}")
        result = None

    SESSION_CACHE[cache_key] = result
    if not result: logging.warning(f"Не удалось найти последнюю версию для загрузчика {loader}.")
    return result

def download_mod(url: str, folder: str, filename: str, retries: int = 5, delay: int = 3, mod_name: str = "unknown", progress_callback=None) -> bool:
    """Скачивает файл по URL, с возможностью повторных попыток при ошибке."""
    filepath = os.path.join(folder, filename)

    for attempt in range(retries):
        try:
            logging.info(f"Попытка скачивания {filename} (попытка {attempt + 1}/{retries})...")
            logging.debug(f"URL для скачивания: {url}")
            # Устанавливаем таймаут, чтобы не ждать вечно при зависании сети
            with SESSION.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                total_size = int(r.headers.get('content-length', 0))
                downloaded_size = 0
                logging.info(f"Успешное соединение для скачивания {filename} (код ответа: {r.status_code}, размер: {total_size} байт).")

                with open(filepath, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk: # filter out keep-alive new chunks
                            f.write(chunk)
                            downloaded_size += len(chunk)
                            if progress_callback and total_size > 0:
                                progress_callback(downloaded_size, total_size)
            
            # Проверяем, что скачанный файл не пустой
            if os.path.getsize(filepath) > 0:
                logging.info(f"Файл {filename} успешно скачан (размер: {os.path.getsize(filepath)} байт).")
                return True  # Успех
            else:
                logging.warning(f"Скачанный файл {filename} пуст (0 байт). Считаем это ошибкой.")
                # Искусственно вызываем ошибку, чтобы попасть в блок except для очистки и повтора
                raise IOError("Downloaded file is empty")

        except (requests.RequestException, IOError, OSError) as e:
            logging.warning(f"Ошибка скачивания или записи файла {filename} на попытке {attempt + 1}: {e}")
            if os.path.exists(filepath):
                try:
                    os.remove(filepath) # Удаляем частично скачанный файл
                except OSError as remove_error:
                    logging.error(f"Не удалось удалить частично скачанный файл {filepath}: {remove_error}")

            if attempt < retries - 1:
                # Экспоненциальная задержка (3s, 6s, 12s, 24s), чтобы быть "вежливее" к серверу
                current_delay = delay * (2 ** attempt)
                logging.info(f"Ожидание {current_delay} секунд перед следующей попыткой...")
                time.sleep(current_delay)

    logging.error(f"Не удалось скачать {filename} после {retries} попыток.")
    return False

def backup_and_remove_mod(mod_path: str):
    """Создает бэкап старого файла мода и удаляет его."""
    # Главная папка для бэкапов внутри папки mods
    main_backup_dir = os.path.join(os.path.dirname(mod_path), "ModUpdater_Backups")
    # Подпапка для одиночных файлов
    file_backup_dir = os.path.join(main_backup_dir, "file_backups")

    os.makedirs(file_backup_dir, exist_ok=True)

    backup_path = os.path.join(file_backup_dir, os.path.basename(mod_path))
    # Если бэкап уже существует, удаляем его, чтобы создать новый
    if os.path.exists(backup_path):
        os.remove(backup_path)

    shutil.move(mod_path, backup_path)
    logging.info(f"Старый файл {os.path.basename(mod_path)} перемещен в бэкап: {file_backup_dir}")
