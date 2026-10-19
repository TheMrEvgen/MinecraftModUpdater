import os
import datetime
import logging
import re
import json
import zipfile
import sqlite3
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None
import concurrent.futures
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal
from packaging.version import parse as parse_version

from mod_updater_logic import (
    get_file_hash, get_mods_info_from_hashes, get_latest_version,
    download_mod, backup_and_remove_mod, get_latest_loader_version, get_projects_info_from_ids
)


class ScanWorker(QThread):

    """Поток для сканирования модов и проверки обновлений."""
    finished = pyqtSignal(list)
    progress = pyqtSignal(str)

    def __init__(self, mods_path, game_version, loader, cache_file):
        super().__init__()
        self.mods_path = mods_path
        self.game_version = game_version
        self.loader = loader
        self.is_running = True
        self.cache_file = cache_file

    def _get_mod_update_info(self, latest_version_info, current_version_number):
        """Helper to create update details dict from latest version info."""
        update_info = {}
        # Если current_version_number is None, значит мод не установлен, и мы ищем инфо для установки
        if latest_version_info and latest_version_info.get('version_number') != current_version_number:
            primary_file = next((f for f in latest_version_info.get('files', []) if f['primary']), None)
            if primary_file:
                update_info["status"] = "Доступно обновление"
                update_info["new_version"] = latest_version_info['version_number']
                if date_str := latest_version_info.get('date_published', ''):
                    try:
                        update_info["new_version_date"] = datetime.datetime.fromisoformat(date_str.replace('Z', '+00:00')).strftime('%Y-%m-%d')
                    except ValueError: pass
                update_info["download_url"] = primary_file['url']
                update_info["new_filename"] = primary_file['filename']
        return update_info

    def run(self):
        mods_data = []
        jar_files = [f for f in os.listdir(self.mods_path) if f.endswith('.jar')]
        if not jar_files:
            self.finished.emit([])
            return

        self.progress.emit(f"Найдено {len(jar_files)} .jar файлов. Вычисление хэшей...")

        hashes = {} # sha1 -> filepath
        filepaths = [os.path.join(self.mods_path, f) for f in jar_files]
        
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future_to_path = {executor.submit(get_file_hash, path): path for path in filepaths}
            for future in concurrent.futures.as_completed(future_to_path):
                if not self.is_running:
                    executor.shutdown(wait=False, cancel_futures=True)
                    self.finished.emit([])
                    return
                path = future_to_path[future]
                hashes[future.result()] = path

        # --- Загрузка и использование кэша (SQLite) ---
        cached_mod_info = {}
        os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
        
        conn = None
        try:
            conn = sqlite3.connect(self.cache_file)
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS mod_info (
                    sha1 TEXT PRIMARY KEY,
                    data TEXT
                )
            ''')
            conn.commit()

            if hashes:
                hash_keys = list(hashes.keys())
                CHUNK_SIZE = 900
                for i in range(0, len(hash_keys), CHUNK_SIZE):
                    chunk = hash_keys[i:i + CHUNK_SIZE]
                    placeholders = ','.join(['?'] * len(chunk))
                    cursor.execute(f"SELECT sha1, data FROM mod_info WHERE sha1 IN ({placeholders})", chunk)
                    for row in cursor.fetchall():
                        sha1, data_json = row
                        try:
                            cached_mod_info[sha1] = json.loads(data_json)
                        except json.JSONDecodeError:
                            pass
        except sqlite3.Error as e:
            logging.error(f"Ошибка при работе с SQLite кэшем: {e}")

        mods_info_from_modrinth = {}
        hashes_to_fetch = []
        
        for sha1, filepath in hashes.items():
            if not self.is_running:
                if conn: conn.close()
                self.finished.emit([])
                return
            if sha1 in cached_mod_info:
                mods_info_from_modrinth[sha1] = cached_mod_info[sha1]
                logging.debug(f"Используем кэшированную информацию для мода с SHA1: {sha1}")
            else:
                hashes_to_fetch.append(sha1)

        if hashes_to_fetch:
            self.progress.emit(f"Запрос информации о {len(hashes_to_fetch)} новых модах с Modrinth...")
            fetched_info = get_mods_info_from_hashes(hashes_to_fetch)
            if not self.is_running:
                if conn: conn.close()
                self.finished.emit([])
                return
            mods_info_from_modrinth.update(fetched_info)
            
            if conn:
                try:
                    cursor = conn.cursor()
                    cursor.execute("BEGIN TRANSACTION")
                    for sha1, info in fetched_info.items():
                        cursor.execute("INSERT OR REPLACE INTO mod_info (sha1, data) VALUES (?, ?)", (sha1, json.dumps(info)))
                    conn.commit()
                    logging.info(f"Кэш SQLite успешно обновлен ({len(fetched_info)} новых записей).")
                except sqlite3.Error as e:
                    logging.error(f"Ошибка при сохранении в SQLite кэш: {e}")
        else:
            self.progress.emit("Все данные о модах найдены в кэше.")
            logging.info("Все данные о модах найдены в кэше, запросы к Modrinth не требуются.")

        if conn: conn.close()

        if not mods_info_from_modrinth:
            self.progress.emit("Не удалось получить информацию о модах. Проверьте интернет-соединение.")
            self.finished.emit([])
            return

        # --- Build detailed map of installed projects ---
        installed_project_ids = set()
        installed_project_details = {}  # project_id -> {version_number, filepath, sha1}
        for sha1, info in mods_info_from_modrinth.items():
            if 'project_id' in info:
                project_id = info['project_id']
                installed_project_ids.add(project_id)
                installed_project_details[project_id] = {
                    'version_number': info['version_number'],
                    'filepath': hashes.get(sha1),
                    'sha1': sha1,
                    'info': info
                }

        # --- Fetch latest versions for all installed mods ---
        self.progress.emit(f"Проверка обновлений для {len(installed_project_ids)} модов...")
        latest_versions_map = {}  # {project_id: version_info}
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_to_project = {
                executor.submit(get_latest_version, pid, self.game_version, self.loader): pid
                for pid in installed_project_ids
            }
            for future in concurrent.futures.as_completed(future_to_project):
                if not self.is_running:
                    executor.shutdown(wait=False, cancel_futures=True)
                    self.finished.emit([])
                    return
                project_id = future_to_project[future]
                try:
                    latest_versions_map[project_id] = future.result()
                except Exception as e:
                    logging.error(f"Не удалось получить последнюю версию для проекта {project_id} после нескольких попыток: {e}")
                    latest_versions_map[project_id] = None # Устанавливаем None при ошибке

        # --- Find all dependencies and fetch info for them ---
        all_dependency_project_ids = set()
        dependency_requirements = {} # dep_project_id -> set of required version_ids
        for project_id in installed_project_ids:
            version_info = latest_versions_map.get(project_id)
            if version_info and 'dependencies' in version_info:
                for dep in version_info['dependencies']:
                    if dep.get('project_id'):
                        all_dependency_project_ids.add(dep['project_id'])
                        dep_vid = dep.get('version_id')
                        if dep_vid:
                            if dep['project_id'] not in dependency_requirements:
                                dependency_requirements[dep['project_id']] = set()
                            dependency_requirements[dep['project_id']].add(dep_vid)

        missing_dependency_ids = all_dependency_project_ids - installed_project_ids
        if missing_dependency_ids:
            self.progress.emit(f"Поиск информации о {len(missing_dependency_ids)} недостающих зависимостях...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                future_to_project = {
                    executor.submit(get_latest_version, pid, self.game_version, self.loader): pid
                    for pid in missing_dependency_ids
                }
                for future in concurrent.futures.as_completed(future_to_project):
                    if not self.is_running:
                        executor.shutdown(wait=False, cancel_futures=True)
                        return
                    project_id = future_to_project[future]
                    try:
                        latest_versions_map[project_id] = future.result()
                    except Exception as e:
                        logging.error(f"Не удалось получить последнюю версию для зависимости {project_id}: {e}")

        dependency_projects_info = {}
        if all_dependency_project_ids:
            self.progress.emit(f"Запрос названий для {len(all_dependency_project_ids)} зависимостей...")
            dependency_projects_info = get_projects_info_from_ids(list(all_dependency_project_ids))

        # --- Build the final mods_data list ---
        standalone_project_ids = installed_project_ids - all_dependency_project_ids
        total_mods_to_process = len(standalone_project_ids)
        
        for i, project_id in enumerate(standalone_project_ids):
            if not self.is_running: return

            details = installed_project_details[project_id]
            filepath = details['filepath']
            mod_name, _, _ = _get_mod_metadata(filepath) if filepath else (project_id, "", None)
            self.progress.emit(f"Обработка: {i + 1} / {total_mods_to_process} ({mod_name})")

            current_version_number = details['version_number']
            latest_version_info = latest_versions_map.get(project_id)

            mod_entry = { 
                "name": mod_name, 
                "project_id": project_id, 
                "current_version": current_version_number, 
                "filepath": filepath, 
                "status": "Актуально", 
                "new_version": "", 
                "new_version_date": "", 
                "download_url": None, 
                "new_filename": None,
                "installed_info": details.get('info') 
            }

            update_details = self._get_mod_update_info(latest_version_info, current_version_number)
            if update_details:
                mod_entry.update(update_details)
                logging.info(f"Найдено обновление для '{mod_name}': {current_version_number} -> {mod_entry['new_version']}.")
            else:
                logging.info(f"Мод '{mod_name}' (версия: {current_version_number}) актуален.")

            mods_data.append(mod_entry)

        for project_id in installed_project_ids:
            version_info = latest_versions_map.get(project_id)
            if version_info and 'dependencies' in version_info:
                dependencies_list = []
                for dep in version_info['dependencies']:
                    dep_project_id = dep.get('project_id')
                    if not dep_project_id:
                        continue

                    dep_name = dependency_projects_info.get(dep_project_id, {}).get('title', dep_project_id)
                    is_installed = dep_project_id in installed_project_ids
                    dep_latest_version = latest_versions_map.get(dep_project_id)

                    dep_entry = {
                        'name': dep_name, 'project_id': dep_project_id, 'type': dep.get('dependency_type', 'unknown'),
                        'installed': is_installed, 'current_version': "", 'new_version': "", 'new_version_date': "",
                        'status': "", 'download_url': None, 'new_filename': None, 'filepath': None, 'installed_info': None
                    }

                    if is_installed:
                        installed_dep = installed_project_details[dep_project_id]
                        dep_entry['current_version'] = installed_dep['version_number']
                        dep_entry['filepath'] = installed_dep['filepath']
                        dep_entry['installed_info'] = installed_dep.get('info')
                        dep_entry['status'] = "Актуально"
                        
                        if dep_project_id in dependency_requirements and len(dependency_requirements[dep_project_id]) > 1:
                            dep_entry['status'] = "⚠️ Конфликт версий"
                        else:
                            dep_update_details = self._get_mod_update_info(dep_latest_version, installed_dep['version_number'])
                            if dep_update_details:
                                dep_entry.update(dep_update_details)
                    else: # Not installed
                        dep_entry['status'] = "Требуется" if dep_entry['type'] == 'required' else "Опционально"
                        if dep_latest_version:
                            if dep_project_id in dependency_requirements and len(dependency_requirements[dep_project_id]) > 1:
                                dep_entry['status'] = "⚠️ Конфликт версий"
                            else:
                                # We don't have a "current_version", so pass None to get install info
                                install_details = self._get_mod_update_info(dep_latest_version, None)
                                if install_details:
                                    dep_entry.update(install_details)
                                    # Status should reflect it's a dependency, not a simple update
                                    dep_entry['status'] = "Требуется" if dep_entry['type'] == 'required' else "Опционально"
                    
                    dependencies_list.append(dep_entry)

                # Привязываем зависимости к основному моду
                for mod in mods_data:
                    if mod['project_id'] == project_id:
                        mod['dependencies'] = dependencies_list
                        break

        # --- Processing mods not found on Modrinth ---
        unmatched_filepaths = []
        for sha1, filepath in hashes.items():
            if sha1 not in mods_info_from_modrinth or 'project_id' not in mods_info_from_modrinth[sha1]:
                unmatched_filepaths.append(filepath)

        for filepath in unmatched_filepaths:
            if not self.is_running: return
            mod_name, mod_version, _ = _get_mod_metadata(filepath) if filepath else (os.path.basename(filepath), "", None)
            
            mod_entry = {
                "name": mod_name, 
                "project_id": None, 
                "current_version": mod_version, 
                "filepath": filepath, 
                "status": "Не найден на Modrinth", 
                "new_version": "", 
                "new_version_date": "", 
                "download_url": None, 
                "new_filename": None 
            }
            mods_data.append(mod_entry)

        self.finished.emit(mods_data)

    def stop(self):
        self.is_running = False
        logging.info("Получен запрос на остановку сканирования.")


class UpdateWorker(QThread):
    """Поток для скачивания и установки обновлений."""
    finished = pyqtSignal()
    progress = pyqtSignal(int, str) # row, status
    download_progress = pyqtSignal(int, int, int) # mod_index, downloaded, total

    def __init__(self, mods_to_update, mods_path):
        super().__init__()
        self.mods_to_update = mods_to_update
        self.mods_path = mods_path
        # Ограничиваем количество одновременных скачиваний, чтобы не вызывать защиту сервера
        self.MAX_WORKERS = 2
        self.is_running = True

    def stop(self):
        self.is_running = False
        logging.info("Получен запрос на остановку обновления.")

    def _process_single_mod(self, i, mod):
        """Обрабатывает обновление одного мода. Запускается в отдельном потоке из пула."""
        if not self.is_running:
            self.progress.emit(i, "Отменено")
            return

        mod_name = mod['name']
        current_version = mod['current_version']
        new_version = mod['new_version']
        new_filename = mod['new_filename']

        try:
            self.progress.emit(i, "Скачивание...")
            logging.info(f"Начинается скачивание мода '{mod_name}' (текущая: {current_version}, новая: {new_version}, файл: {new_filename})...")
            # Скачиваем во временный файл, чтобы не потерять старый мод при ошибке
            temp_filename = new_filename + ".part"

            def callback(downloaded, total):
                if self.is_running:
                    self.download_progress.emit(i, downloaded, total)

            success = download_mod(mod['download_url'], self.mods_path, temp_filename, mod_name=mod_name, progress_callback=callback)
            
            if success:
                try:
                    self.progress.emit(i, "Завершение...")
                    
                    temp_filepath = os.path.join(self.mods_path, temp_filename)
                    final_filepath = os.path.join(self.mods_path, new_filename)

                    # 1. Бэкап старой версии мода, если она есть и путь отличается от нового.
                    #    Если пути совпадают, бэкап произойдет на следующем шаге.
                    if mod['filepath'] and os.path.exists(mod['filepath']) and os.path.normpath(mod['filepath']) != os.path.normpath(final_filepath):
                        logging.info(f"Старое имя файла '{os.path.basename(mod['filepath'])}' отличается от нового. Создание резервной копии...")
                        backup_and_remove_mod(mod['filepath'])

                    # 2. Бэкап любого файла, который находится по пути нового файла.
                    #    Это покроет и случай обновления с тем же именем, и случай импорта с конфликтом имен.
                    if os.path.exists(final_filepath):
                        logging.info(f"Обнаружен существующий файл '{new_filename}'. Создание резервной копии...")
                        backup_and_remove_mod(final_filepath)

                    logging.info(f"Скачивание '{mod_name}' завершено. Перемещение файла...")
                    os.rename(temp_filepath, final_filepath)

                    final_status = "Установлено!" if not mod['filepath'] else "Обновлено!"
                    logging.info(f"Мод '{mod_name}' успешно {final_status.lower().replace('!', '')} до версии {new_version}.")
                    self.progress.emit(i, final_status)
                except Exception as e:
                    logging.error(f"Критическая ошибка при замене файла для мода {mod['name']}: {e}")
                    self.progress.emit(i, "Ошибка замены!")
            else:
                # download_mod уже записал ошибку в лог и удалил временный файл
                self.progress.emit(i, "Ошибка!")
                logging.error(f"Не удалось скачать мод '{mod_name}' (версия: {new_version}).")
        except Exception as e:
            # Отлавливаем любые непредвиденные ошибки в потоке, чтобы он не "умер" молча
            logging.critical(f"Неперехваченная ошибка в потоке обработки мода {mod['name']}: {e}")
            self.progress.emit(i, "Крит. ошибка!")

    def run(self):
        # Используем ThreadPoolExecutor для параллельного скачивания модов
        logging.info(f"Запуск параллельного обновления. Количество потоков: {self.MAX_WORKERS}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.MAX_WORKERS) as executor:
            # Создаем задачи для каждого мода и сохраняем их для отслеживания
            future_to_mod = {
                executor.submit(self._process_single_mod, i, mod): mod['name']
                for i, mod in enumerate(self.mods_to_update)
            }

            # Ожидаем завершения всех задач и логируем возможные ошибки
            for future in concurrent.futures.as_completed(future_to_mod):
                mod_name = future_to_mod[future]
                try:
                    future.result()  # Получаем результат (или исключение, если оно было)
                except Exception as e:
                    logging.error(f"Задача для мода {mod_name} завершилась с ошибкой: {e}")
        
        logging.info("Все задачи по обновлению модов завершены.")
        self.finished.emit()


def _get_mod_metadata(filepath: str) -> tuple[str, str, Optional[str]]:
    """Reads metadata (name, version, project_id) from a mod's jar file."""
    filename = os.path.basename(filepath)
    mod_name = os.path.splitext(filename)[0]
    mod_version = ""
    project_id = None

    if not os.path.exists(filepath):
        return mod_name, mod_version, project_id

    try:
        with zipfile.ZipFile(filepath, 'r') as z:
            # --- Case 1: Fabric mod (simple and authoritative) ---
            if 'fabric.mod.json' in z.namelist():
                with z.open('fabric.mod.json') as f:
                    mod_meta = json.load(f)
                mod_name = mod_meta.get('name', mod_name)
                mod_version = mod_meta.get('version', '')
                if 'custom' in mod_meta and isinstance(mod_meta['custom'], dict) and 'modrinth' in mod_meta['custom']:
                    project_id = mod_meta['custom']['modrinth']
                return mod_name, mod_version, project_id
            
            # --- Case 2: Forge/NeoForge mod (Direct Logic) ---
            
            toml_name = None
            toml_version_str = None

            # Step 1: Find the correct TOML file and read its content.
            toml_path = None
            if tomllib:
                namelist = z.namelist()
                if 'META-INF/neoforge.mods.toml' in namelist:
                    toml_path = 'META-INF/neoforge.mods.toml'
                elif 'META-INF/mods.toml' in namelist:
                    toml_path = 'META-INF/mods.toml'
            
            if toml_path:
                try:
                    with z.open(toml_path) as f:
                        mod_meta = tomllib.load(f)
                        if 'mods' in mod_meta and mod_meta['mods']:
                            first_mod = mod_meta['mods'][0]
                            toml_name = first_mod.get('displayName')
                            toml_version_str = str(first_mod.get('version', ''))
                except Exception as e:
                    logging.warning(f"Could not parse {toml_path} for {filename}: {e}")

            # Step 2: Decide on the final version based on TOML content.
            if toml_version_str and toml_version_str != '${file.jarVersion}':
                # We found a valid, non-placeholder version in the TOML file. This is our answer.
                mod_version = toml_version_str
                logging.debug(f"Using version '{mod_version}' from {toml_path}")
            else:
                # TOML version is a placeholder or missing. We MUST check the manifest.
                logging.debug(f"Version in {toml_path or 'TOML'} is '{toml_version_str}' or missing. Checking MANIFEST.MF.")
                if 'META-INF/MANIFEST.MF' in z.namelist():
                    try:
                        with z.open('META-INF/MANIFEST.MF') as mf_file:
                            manifest_data = mf_file.read().decode('utf-8', errors='ignore')
                            ver_match = re.search(r'^Implementation-Version:\s*(.*)', manifest_data, re.MULTILINE)
                            if ver_match:
                                version_str = ver_match.group(1).strip()
                                if version_str and version_str != '${file.jarVersion}':
                                    mod_version = version_str
                                    logging.debug(f"Using version '{mod_version}' from MANIFEST.MF.")
                    except Exception as e:
                        logging.warning(f"Could not parse MANIFEST.MF for {filename}: {e}")

            # Step 3: Set the name (TOML name has priority)
            if toml_name:
                mod_name = toml_name
            
    except Exception as e:
        logging.warning(f"Не удалось прочитать метаданные из {filename}: {e}")
    
    return mod_name, mod_version, project_id


class LocalScanWorker(QThread):
    """Поток для быстрого локального сканирования модов."""
    finished = pyqtSignal(list)

    def __init__(self, mods_path):
        super().__init__()
        self.mods_path = mods_path
        self.is_running = True

    def run(self):
        mods_data = []
        if tomllib is None:
            logging.warning("Библиотека 'tomli' или 'tomllib' не установлена. Метаданные Forge/NeoForge модов (mods.toml) не будут прочитаны. Для Python < 3.11 установите ее: pip install tomli")

        try:
            jar_files = [os.path.join(self.mods_path, f) for f in os.listdir(self.mods_path) if f.endswith('.jar')]
            
            with concurrent.futures.ThreadPoolExecutor() as executor:
                # Создаем задачи для каждого файла
                future_to_filepath = {executor.submit(_get_mod_metadata, filepath): filepath for filepath in jar_files}
                
                for future in concurrent.futures.as_completed(future_to_filepath):
                    if not self.is_running:
                        logging.info("Локальное сканирование прервано.")
                        executor.shutdown(wait=False, cancel_futures=True)
                        self.finished.emit([])
                        return
                    
                    filepath = future_to_filepath[future]
                    try:
                        mod_name, mod_version, project_id = future.result()
                        mod_entry = { "name": mod_name, "project_id": project_id, "current_version": mod_version, "filepath": filepath, "status": "Не проверено", "new_version": "", "download_url": None, "new_filename": None }
                        mods_data.append(mod_entry)
                    except Exception as e:
                        logging.error(f"Ошибка при чтении метаданных из {os.path.basename(filepath)}: {e}")

            logging.info(f"Локальное сканирование завершено. Найдено {len(mods_data)} .jar файлов.")
        except Exception as e:
            logging.error(f"Ошибка во время локального сканирования: {e}")
        
        self.finished.emit(mods_data)

    def stop(self):
        self.is_running = False
        logging.info("Получен запрос на остановку локального сканирования.")

class ImportWorker(QThread):
    """Поток для получения информации о модах по списку ID."""
    finished = pyqtSignal(list)
    progress = pyqtSignal(str)

    def __init__(self, target_mods, game_version, loader, use_exact_versions=False):
        super().__init__()
        self.target_mods = target_mods
        self.project_ids = list(target_mods.keys()) if isinstance(target_mods, dict) else target_mods
        self.game_version = game_version
        self.loader = loader
        self.use_exact_versions = use_exact_versions
        self.is_running = True

    def run(self):
        mods_data = []
        total_mods = len(self.project_ids)
        self.progress.emit(f"Запрос информации о {total_mods} проектах...")
        
        projects_info = get_projects_info_from_ids(self.project_ids)
        if not projects_info:
            if not self.is_running: logging.info("Импорт прерван пользователем.")
            self.progress.emit("Не удалось получить информацию о проектах.")
            self.finished.emit([])
            return

        self.progress.emit(f"Поиск совместимых версий для {len(projects_info)} модов...")

        # --- Параллельный запрос версий ---
        latest_versions_map = {} # {project_id: version_info}
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_to_project = {}
            for project_id in self.project_ids:
                if project_id in projects_info:
                    target_version = self.target_mods.get(project_id) if self.use_exact_versions and isinstance(self.target_mods, dict) else None
                    future = executor.submit(get_latest_version, project_id, self.game_version, self.loader, target_version)
                    future_to_project[future] = project_id
            logging.info(f"Запущено {len(future_to_project)} задач на получение версий для импорта.")

            for future in concurrent.futures.as_completed(future_to_project):
                if not self.is_running:
                    executor.shutdown(wait=False, cancel_futures=True)
                    self.finished.emit([])
                    return
                project_id = future_to_project[future]
                try:
                    latest_versions_map[project_id] = future.result()
                except Exception as e:
                    logging.error(f"Не удалось получить версию для импорта проекта {project_id} после нескольких попыток: {e}")
        
        if not self.is_running:
            self.finished.emit([])
            return

        for i, project_id in enumerate(self.project_ids):
            if not self.is_running:
                logging.info("Импорт прерван пользователем в процессе поиска версий.")
                self.finished.emit([])
                return

            project_info = projects_info.get(project_id)
            if not project_info:
                logging.warning(f"Не удалось найти информацию для project_id: {project_id}")
                continue

            mod_name = project_info.get('slug', project_id)
            self.progress.emit(f"Обработка: {i + 1} / {total_mods} ({mod_name})")

            latest_version_info = latest_versions_map.get(project_id)

            if latest_version_info:
                primary_file = next((f for f in latest_version_info['files'] if f['primary']), None)
                if primary_file:
                    formatted_date = ""
                    if date_str := latest_version_info.get('date_published', ''):
                        try:
                            formatted_date = datetime.datetime.fromisoformat(date_str.replace('Z', '+00:00')).strftime('%Y-%m-%d')
                        except ValueError:
                            pass
                    mod_entry = { "name": mod_name, "project_id": project_id, "current_version": "", "filepath": None, "status": "Готов к установке", "new_version": latest_version_info['version_number'], "new_version_date": formatted_date, "download_url": primary_file['url'], "new_filename": primary_file['filename'] }
                    mods_data.append(mod_entry)
                    logging.info(f"Найден мод для установки: '{mod_name}' версии {mod_entry['new_version']}.")
            else:
                logging.warning(f"Не найдено совместимых версий для '{mod_name}' (ID: {project_id}).")

        self.finished.emit(mods_data)

    def stop(self):
        self.is_running = False
        logging.info("Получен запрос на остановку импорта.")

class LoaderVersionWorker(QThread):
    """Поток для проверки версии загрузчика."""
    finished = pyqtSignal(str, str) # message, color_key ('ok', 'update', 'error')

    def __init__(self, loader, game_version, current_loader_version):
        super().__init__()
        self.loader = loader
        self.game_version = game_version
        self.current_loader_version = current_loader_version

    def run(self):
        latest_version_str = get_latest_loader_version(self.loader, self.game_version)

        if not latest_version_str:
            self.finished.emit(f"⚠️ ({self.current_loader_version})", 'error')
            logging.warning(f"Не удалось получить последнюю версию для {self.loader}.")
            return

        try:
            current_v = parse_version(self.current_loader_version)
            latest_v = parse_version(latest_version_str)

            if latest_v > current_v:
                message = f"🔼 ({self.current_loader_version} -> {latest_version_str})"
                self.finished.emit(message, 'update')
                logging.info(f"Доступно обновление загрузчика {self.loader}: {self.current_loader_version} -> {latest_version_str}")
            else:
                message = f"✅ ({self.current_loader_version})"
                self.finished.emit(message, 'ok')
                logging.info(f"Загрузчик {self.loader} актуален (версия {self.current_loader_version}).")

        except Exception as e:
            logging.error(f"Ошибка сравнения версий загрузчика: {e}")
            self.finished.emit(f"⚠️ ({self.current_loader_version})", 'error')