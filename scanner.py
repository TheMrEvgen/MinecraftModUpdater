"""
Ядро сканирования модов (без зависимостей от Qt): локальный индекс файлов с кэшем,
запросы к Modrinth и построение списка модов с зависимостями.
Используется и GUI-воркерами, и консольным режимом.
"""
import concurrent.futures
import json
import logging
import os
import re
import sqlite3
import zipfile
from typing import Callable, Dict, List, Optional

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None

import requests

from content import MODS, ContentType, enabled_name, is_disabled
from enums import ModStatus, UpdateChannel
from mod_updater_logic import (
    format_date, get_all_versions, get_file_hash, get_latest_version, get_latest_versions_from_hashes,
    get_mods_info_from_hashes, get_projects_info_from_ids, is_newer_version, select_primary_file,
)

ProgressCallback = Callable[[str], None]
CancelCheck = Callable[[], bool]

# Типы зависимостей, которые означают «этот проект нужен моду»
LINKING_DEPENDENCY_TYPES = ("required", "optional")


class ScanCancelled(Exception):
    """Сканирование прервано пользователем."""


# --- Метаданные jar-файлов ---

def read_mod_metadata(filepath: str) -> tuple[str, str, Optional[str]]:
    """Читает метаданные (name, version, project_id) из jar-файла мода."""
    filename = enabled_name(os.path.basename(filepath))
    mod_name = os.path.splitext(filename)[0]
    mod_version = ""
    project_id = None

    try:
        with zipfile.ZipFile(filepath, 'r') as z:
            namelist = set(z.namelist())

            # --- Fabric / Quilt ---
            for meta_name in ('fabric.mod.json', 'quilt.mod.json'):
                if meta_name in namelist:
                    with z.open(meta_name) as f:
                        mod_meta = json.loads(f.read().decode('utf-8', errors='ignore'), strict=False)
                    if meta_name == 'quilt.mod.json':
                        loader_meta = mod_meta.get('quilt_loader', {})
                        mod_name = loader_meta.get('metadata', {}).get('name', mod_name)
                        mod_version = str(loader_meta.get('version', ''))
                    else:
                        mod_name = mod_meta.get('name', mod_name)
                        mod_version = str(mod_meta.get('version', ''))
                        custom = mod_meta.get('custom')
                        if isinstance(custom, dict) and isinstance(custom.get('modrinth'), str):
                            project_id = custom['modrinth']
                    return mod_name, mod_version, project_id

            # --- Forge / NeoForge ---
            toml_name = None
            toml_version = None
            toml_path = next((p for p in ('META-INF/neoforge.mods.toml', 'META-INF/mods.toml') if p in namelist), None)
            if toml_path and tomllib:
                try:
                    with z.open(toml_path) as f:
                        mod_meta = tomllib.loads(f.read().decode('utf-8', errors='ignore'))
                    mods = mod_meta.get('mods') or []
                    if mods:
                        toml_name = mods[0].get('displayName')
                        toml_version = str(mods[0].get('version', ''))
                except Exception as e:
                    logging.warning(f"Could not parse {toml_path} for {filename}: {e}")

            if toml_version and toml_version != '${file.jarVersion}':
                mod_version = toml_version
            elif 'META-INF/MANIFEST.MF' in namelist:
                # Версия в TOML - плейсхолдер или отсутствует, смотрим манифест
                manifest = z.read('META-INF/MANIFEST.MF').decode('utf-8', errors='ignore')
                match = re.search(r'^Implementation-Version:\s*(.*)', manifest, re.MULTILINE)
                if match and match.group(1).strip() not in ('', '${file.jarVersion}'):
                    mod_version = match.group(1).strip()

            if toml_name:
                mod_name = toml_name
    except Exception as e:
        logging.warning(f"Не удалось прочитать метаданные из {filename}: {e}")

    return mod_name, mod_version, project_id


# --- Кэш (SQLite) ---

class ModCache:
    """
    Постоянный кэш:
      * file_index — SHA1 и метаданные файла по (путь, размер, mtime): неизменённые jar не перечитываются;
      * mod_info   — ответ Modrinth для версии по SHA1 (данные версии неизменны).
    """

    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS mod_info (sha1 TEXT PRIMARY KEY, data TEXT);
            CREATE TABLE IF NOT EXISTS project_info (project_id TEXT PRIMARY KEY, data TEXT);
            CREATE TABLE IF NOT EXISTS file_index (
                path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER,
                sha1 TEXT, name TEXT, version TEXT, project_hint TEXT
            );
        ''')
        self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def get_file_index(self) -> Dict[str, tuple]:
        rows = self.conn.execute("SELECT path, size, mtime_ns, sha1, name, version, project_hint FROM file_index")
        return {row[0]: row[1:] for row in rows}

    def put_file_entries(self, entries: List[Dict]):
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO file_index VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(e['path'], e['size'], e['mtime_ns'], e['sha1'], e['name'], e['version'], e['project_hint'])
                 for e in entries])

    def get_mod_infos(self, hashes: List[str]) -> Dict[str, Dict]:
        result = {}
        for i in range(0, len(hashes), 900):  # ограничение SQLite на число параметров
            chunk = hashes[i:i + 900]
            placeholders = ','.join('?' * len(chunk))
            for sha1, data in self.conn.execute(f"SELECT sha1, data FROM mod_info WHERE sha1 IN ({placeholders})", chunk):
                try:
                    result[sha1] = json.loads(data)
                except json.JSONDecodeError:
                    pass
        return result

    def get_project_infos(self, project_ids) -> Dict[str, Dict]:
        ids = list(project_ids)
        result = {}
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            placeholders = ','.join('?' * len(chunk))
            query = f"SELECT project_id, data FROM project_info WHERE project_id IN ({placeholders})"
            for pid, data in self.conn.execute(query, chunk):
                try:
                    result[pid] = json.loads(data)
                except json.JSONDecodeError:
                    pass
        return result

    def put_project_infos(self, infos: Dict[str, Dict]):
        # Храним только поля, нужные интерфейсу: ответ /projects довольно объёмный
        keys = ('id', 'slug', 'title', 'description', 'icon_url', 'client_side', 'server_side', 'project_type',
                'categories')
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO project_info (project_id, data) VALUES (?, ?)",
                                  [(pid, json.dumps({k: info.get(k) for k in keys})) for pid, info in infos.items()])

    def put_mod_infos(self, infos: Dict[str, Dict]):
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO mod_info (sha1, data) VALUES (?, ?)",
                                  [(sha1, json.dumps(info)) for sha1, info in infos.items()])


def _index_file(path: str, size: int, mtime_ns: int) -> Dict:
    if enabled_name(path).lower().endswith('.jar'):
        name, version, project_hint = read_mod_metadata(path)
    else:  # Шейдеры и ресурспаки: метаданных нет, название — имя файла
        name, version, project_hint = os.path.splitext(enabled_name(os.path.basename(path)))[0], "", None
    return {'path': path, 'size': size, 'mtime_ns': mtime_ns, 'sha1': get_file_hash(path),
            'name': name, 'version': version, 'project_hint': project_hint}


def list_content_files(folder: str, content: ContentType = MODS) -> List[str]:
    """Файлы контента в папке, включая выключенные (*.disabled)."""
    if not os.path.isdir(folder):
        return []
    return sorted(os.path.join(folder, f) for f in os.listdir(folder)
                  if content.matches(f) and os.path.isfile(os.path.join(folder, f)))


def list_jar_files(mods_path: str) -> List[str]:
    return list_content_files(mods_path, MODS)


def index_local_files(mods_path: str, cache: Optional[ModCache] = None,
                      is_cancelled: CancelCheck = lambda: False, content: ContentType = MODS) -> List[Dict]:
    """
    Возвращает список {path, sha1, name, version, project_hint} для всех файлов контента в папке.
    Хэш и метаданные пересчитываются только для новых или изменённых файлов.
    """
    cached = cache.get_file_index() if cache else {}
    result: List[Dict] = []
    to_index = []
    for path in list_content_files(mods_path, content):
        try:
            st = os.stat(path)
        except OSError:
            continue
        entry = cached.get(path)
        if entry and entry[0] == st.st_size and entry[1] == st.st_mtime_ns:
            result.append({'path': path, 'size': st.st_size, 'mtime_ns': st.st_mtime_ns, 'sha1': entry[2],
                           'name': entry[3], 'version': entry[4], 'project_hint': entry[5]})
        else:
            to_index.append((path, st.st_size, st.st_mtime_ns))

    if to_index:
        logging.info(f"Индексация {len(to_index)} новых или изменённых файлов (из кэша: {len(result)}).")
        new_entries = []
        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = [executor.submit(_index_file, *args) for args in to_index]
            for future in concurrent.futures.as_completed(futures):
                if is_cancelled():
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise ScanCancelled()
                try:
                    new_entries.append(future.result())
                except OSError as e:
                    logging.error(f"Не удалось прочитать файл: {e}")
        if cache:
            cache.put_file_entries(new_entries)
        result.extend(new_entries)

    result.sort(key=lambda e: e['path'].lower())
    return result


# --- Построение записей ---

def make_local_entry(local: Dict, status: ModStatus = ModStatus.NOT_CHECKED) -> Dict:
    return {
        "name": local['name'], "project_id": local.get('project_hint'), "current_version": local['version'],
        "filepath": local['path'], "sha1": local.get('sha1'), "status": status, "new_version": "",
        "new_version_date": "", "download_url": None, "new_filename": None, "new_hashes": None,
        "size": local.get('size') or 0, "disabled": is_disabled(local['path']),
    }


def apply_install_info(entry: Dict, version_info: Optional[Dict]) -> bool:
    """Заполняет поля для скачивания версии version_info. Возвращает False, если файла нет."""
    primary_file = select_primary_file(version_info)
    if not primary_file:
        return False
    entry["new_version"] = version_info['version_number']
    entry["new_version_date"] = format_date(version_info.get('date_published'))
    entry["download_url"] = primary_file['url']
    entry["new_filename"] = primary_file['filename']
    entry["new_hashes"] = primary_file.get('hashes')
    entry["new_size"] = primary_file.get('size') or 0
    entry["_latest_info"] = version_info
    return True


def _apply_project_info(entry: Dict, project: Optional[Dict]):
    if not project:
        return
    if project.get('title'):
        entry["name"] = project['title']
    entry["description"] = project.get('description') or ""
    entry["icon_url"] = project.get('icon_url')
    entry["client_side"] = project.get('client_side')
    entry["server_side"] = project.get('server_side')
    entry["slug"] = project.get('slug')
    entry["categories"] = list(project.get('categories') or [])
    entry["project_type"] = project.get('project_type')


def scan_local(mods_path: str, cache_file: Optional[str], content: ContentType = MODS) -> List[Dict]:
    """
    Быстрое локальное сканирование без сети. Если мод уже проверялся раньше, данные о нём
    (название, иконка, описание, версия на Modrinth) берутся из кэша — интерфейс сразу
    показывает знакомые моды, а сетевая проверка лишь дополняет статусы.
    """
    cache = ModCache(cache_file) if cache_file else None
    try:
        local_files = index_local_files(mods_path, cache, content=content)
        entries = [make_local_entry(local) for local in local_files]
        if not cache:
            return entries
        infos = cache.get_mod_infos([e['sha1'] for e in entries if e.get('sha1')])
        projects = cache.get_project_infos({i['project_id'] for i in infos.values() if i.get('project_id')})
        for entry in entries:
            info = infos.get(entry.get('sha1'))
            if info and info.get('project_id'):
                entry["project_id"] = info['project_id']
                entry["current_version"] = info.get('version_number', entry["current_version"])
                entry["installed_info"] = info
                _apply_project_info(entry, projects.get(info['project_id']))
        return entries
    finally:
        if cache:
            cache.close()


def scan_mods(mods_path: str, game_version: str, loader: str, cache_file: Optional[str],
              channel: UpdateChannel = UpdateChannel.RELEASE,
              progress: ProgressCallback = lambda msg: None,
              is_cancelled: CancelCheck = lambda: False, content: ContentType = MODS) -> List[Dict]:
    """
    Полное сканирование: индексация файлов, определение модов на Modrinth, поиск обновлений
    в выбранном канале и построение дерева зависимостей (для шейдеров и ресурспаков — без зависимостей).
    """
    loaders = content.loaders(loader)
    def check_cancel():
        if is_cancelled():
            raise ScanCancelled()

    cache = ModCache(cache_file) if cache_file else None
    try:
        progress("Индексация локальных файлов...")
        local_files = index_local_files(mods_path, cache, is_cancelled, content)
        if not local_files:
            return []
        by_sha1 = {f['sha1']: f for f in local_files}
        hashes = list(by_sha1)
        check_cancel()

        # --- Информация об установленных версиях (кэш + Modrinth) ---
        installed_infos = cache.get_mod_infos(hashes) if cache else {}
        to_fetch = [h for h in hashes if h not in installed_infos]
        if to_fetch:
            progress(f"Запрос информации о {len(to_fetch)} модах с Modrinth...")
            fetched = get_mods_info_from_hashes(to_fetch)
            if cache and fetched:
                cache.put_mod_infos(fetched)
            installed_infos.update(fetched)
        check_cancel()

        if not installed_infos:
            progress("Не удалось получить информацию о модах. Проверьте интернет-соединение.")

        # project_id -> данные установленного файла. Если файлов одного проекта несколько (дубликаты),
        # основным считается самый новый, остальные показываются отдельно с предупреждением
        installed: Dict[str, Dict] = {}
        duplicates: Dict[str, List[Dict]] = {}
        for sha1, info in installed_infos.items():
            if sha1 in by_sha1 and info.get('project_id'):
                pid = info['project_id']
                details = {'sha1': sha1, 'info': info, 'local': by_sha1[sha1]}
                current = installed.get(pid)
                if current and (current['info'].get('date_published') or '') >= (info.get('date_published') or ''):
                    duplicates.setdefault(pid, []).append(details)
                    continue
                if current:
                    duplicates.setdefault(pid, []).append(current)
                installed[pid] = details

        # --- Последние версии одним пакетным запросом ---
        latest_by_project: Dict[str, Optional[Dict]] = {}
        if installed:
            progress(f"Проверка обновлений для {len(installed)} модов...")
            try:
                latest_by_hash = get_latest_versions_from_hashes(
                    [d['sha1'] for d in installed.values()], game_version, loaders, channel.version_types)
            except requests.RequestException as e:
                logging.error(f"Не удалось получить последние версии: {e}")
                latest_by_hash = {}
            for project_id, details in installed.items():
                latest_by_project[project_id] = latest_by_hash.get(details['sha1'])
        check_cancel()

        # --- Зависимости ---
        def deps_of(project_id: str) -> List[Dict]:
            if not content.has_dependencies:
                return []
            version = latest_by_project.get(project_id) or installed[project_id]['info']
            return [d for d in (version.get('dependencies') or []) if d.get('project_id')]

        dependency_ids = set()
        dependency_requirements: Dict[str, set] = {}
        for project_id in installed:
            for dep in deps_of(project_id):
                dep_type = dep.get('dependency_type')
                if dep_type in LINKING_DEPENDENCY_TYPES:
                    dependency_ids.add(dep['project_id'])
                if dep_type == 'required' and dep.get('version_id'):
                    dependency_requirements.setdefault(dep['project_id'], set()).add(dep['version_id'])

        missing_ids = {pid for pid in dependency_ids if pid not in installed}
        if missing_ids:
            progress(f"Поиск {len(missing_ids)} недостающих зависимостей...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                futures = {executor.submit(get_latest_version, pid, game_version, loaders, None, channel.version_types): pid
                           for pid in missing_ids}
                for future in concurrent.futures.as_completed(futures):
                    if is_cancelled():
                        executor.shutdown(wait=False, cancel_futures=True)
                        raise ScanCancelled()
                    pid = futures[future]
                    try:
                        latest_by_project[pid] = future.result()
                    except Exception as e:
                        logging.error(f"Не удалось получить версию для зависимости {pid}: {e}")
                        latest_by_project[pid] = None

        # --- Информация о проектах: названия, иконки, стороны (клиент/сервер) ---
        projects_info: Dict[str, Dict] = {}
        all_project_ids = set(installed) | dependency_ids | set(duplicates)
        if all_project_ids:
            progress(f"Запрос информации о {len(all_project_ids)} проектах...")
            try:
                projects_info = get_projects_info_from_ids(all_project_ids)
                if cache and projects_info:
                    cache.put_project_infos(projects_info)
            except requests.RequestException as e:
                logging.error(f"Не удалось получить информацию о проектах: {e}")
                if cache:
                    projects_info = cache.get_project_infos(all_project_ids)
        check_cancel()

        # --- Основные моды: установленные, которые не являются зависимостью других ---
        standalone = [pid for pid in installed if pid not in dependency_ids]
        # Защита от циклов (A зависит от B, B от A): мод, родители которого тоже скрыты, показываем в корне
        nested_under_standalone = set()
        for pid in standalone:
            nested_under_standalone.update(d['project_id'] for d in deps_of(pid)
                                           if d.get('dependency_type') in LINKING_DEPENDENCY_TYPES)
        standalone += [pid for pid in installed if pid in dependency_ids and pid not in nested_under_standalone]

        def build_installed_entry(project_id: str) -> Dict:
            details = installed[project_id]
            entry = make_local_entry(details['local'], ModStatus.UP_TO_DATE)
            entry["project_id"] = project_id
            entry["current_version"] = details['info'].get('version_number', entry["current_version"])
            entry["installed_info"] = details['info']
            _apply_project_info(entry, projects_info.get(project_id))
            if project_id in duplicates:
                entry["duplicates"] = [os.path.basename(d['local']['path']) for d in duplicates[project_id]]
            latest = latest_by_project.get(project_id)
            if is_newer_version(latest, details['info']) and apply_install_info(entry, latest):
                entry["status"] = ModStatus.UPDATE_AVAILABLE
                logging.info(f"Найдено обновление для '{entry['name']}': {entry['current_version']} -> {entry['new_version']}.")
            return entry

        mods_data: List[Dict] = []
        def sort_name(pid):
            return (projects_info.get(pid, {}).get('title') or installed[pid]['local']['name']).lower()

        for project_id in sorted(standalone, key=sort_name):
            entry = build_installed_entry(project_id)
            dependencies = []
            for dep in deps_of(project_id):
                dep_pid = dep['project_id']
                dep_type = dep.get('dependency_type', 'required')
                is_installed = dep_pid in installed
                # Встроенные зависимости уже внутри jar, а несовместимые показываем, только если они установлены
                if dep_type == 'embedded' or (dep_type == 'incompatible' and not is_installed):
                    continue
                project = projects_info.get(dep_pid, {})
                if is_installed:
                    dep_entry = build_installed_entry(dep_pid)
                else:
                    dep_entry = {
                        "name": project.get('title', dep_pid), "project_id": dep_pid, "current_version": "",
                        "filepath": None, "status": ModStatus.REQUIRED if dep_type == 'required' else ModStatus.OPTIONAL,
                        "new_version": "", "new_version_date": "", "download_url": None, "new_filename": None,
                        "new_hashes": None, "installed_info": None,
                    }
                    _apply_project_info(dep_entry, project)
                    apply_install_info(dep_entry, latest_by_project.get(dep_pid))
                dep_entry["type"] = dep_type
                dep_entry["installed"] = is_installed

                if dep_type == 'incompatible':
                    dep_entry["status"] = ModStatus.INCOMPATIBLE
                    dep_entry["download_url"] = None
                elif len(dependency_requirements.get(dep_pid, ())) > 1:
                    dep_entry["status"] = ModStatus.VERSION_CONFLICT
                    dep_entry["download_url"] = None
                dependencies.append(dep_entry)
            entry["dependencies"] = dependencies
            mods_data.append(entry)

        # --- Лишние копии одного мода ---
        for project_id, extra in duplicates.items():
            main_name = os.path.basename(installed[project_id]['local']['path'])
            for details in extra:
                entry = make_local_entry(details['local'], ModStatus.UP_TO_DATE)
                entry["project_id"] = project_id
                entry["current_version"] = details['info'].get('version_number', entry["current_version"])
                entry["installed_info"] = details['info']
                _apply_project_info(entry, projects_info.get(project_id))
                entry["duplicate_of"] = main_name
                mods_data.append(entry)

        # --- Моды, не найденные на Modrinth ---
        for local in local_files:
            info = installed_infos.get(local['sha1'])
            if not info or not info.get('project_id'):
                entry = make_local_entry(local, ModStatus.NOT_FOUND)
                entry["project_id"] = None
                mods_data.append(entry)

        return mods_data
    finally:
        if cache:
            cache.close()


# --- Импорт .mrpack ---

def read_mrpack(path: str) -> Dict:
    """Читает modrinth.index.json из .mrpack. Выбрасывает ValueError при некорректном файле."""
    try:
        with zipfile.ZipFile(path) as z:
            index = json.loads(z.read("modrinth.index.json").decode("utf-8"))
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"Не удалось прочитать .mrpack: {e}") from e
    if not isinstance(index, dict) or not isinstance(index.get("files"), list):
        raise ValueError("Некорректный modrinth.index.json")
    return index


def mrpack_game_info(index: Dict) -> tuple[Optional[str], Optional[str]]:
    """(версия игры, загрузчик) из раздела dependencies индекса .mrpack."""
    deps = index.get("dependencies") or {}
    loader = next((name for key, name in (("fabric-loader", "fabric"), ("quilt-loader", "quilt"),
                                          ("neoforge", "neoforge"), ("forge", "forge")) if key in deps), None)
    return deps.get("minecraft"), loader


def entries_from_mrpack(index: Dict) -> List[Dict]:
    """
    Записи «к установке» для модов из .mrpack. Файлы берутся ровно те, что указаны в сборке
    (с проверкой хэшей), а названия и иконки подтягиваются с Modrinth по SHA1.
    Файлы вне папки mods (ресурспаки, шейдеры), серверные моды и папка overrides пропускаются.
    """
    files = [f for f in index["files"] if str(f.get("path", "")).replace("\\", "/").startswith("mods/")
             and f.get("downloads") and (f.get("env") or {}).get("client") != "unsupported"]
    sha1_list = [f["hashes"]["sha1"] for f in files if f.get("hashes", {}).get("sha1")]
    try:
        versions = get_mods_info_from_hashes(sha1_list)
        projects = get_projects_info_from_ids({v.get('project_id') for v in versions.values()})
    except requests.RequestException as e:
        logging.warning(f"Не удалось получить информацию о модах сборки: {e}")
        versions, projects = {}, {}

    entries = []
    for f in files:
        filename = os.path.basename(f["path"])
        version = versions.get(f.get("hashes", {}).get("sha1"), {})
        entry = {
            "name": os.path.splitext(filename)[0], "project_id": version.get("project_id"),
            "current_version": "", "filepath": None, "status": ModStatus.READY_TO_INSTALL,
            "new_version": version.get("version_number", ""),
            "new_version_date": format_date(version.get("date_published")),
            "download_url": f["downloads"][0], "new_filename": filename, "new_hashes": f.get("hashes"),
            "_latest_info": version or None,
        }
        _apply_project_info(entry, projects.get(version.get("project_id")))
        entries.append(entry)
    return entries


# --- Каталог: установка проекта вместе с обязательными зависимостями ---

def resolve_project_install(project_id: str, game_version: str, loader: Optional[str], channel: UpdateChannel,
                            installed_ids=(), content: ContentType = MODS, max_depth: int = 3) -> List[Dict]:
    """
    Записи «к установке» для проекта из каталога и его обязательных зависимостей, которых ещё нет в сборке.
    Возвращает пустой список, если совместимой версии нет. Первая запись — сам проект.
    """
    loaders = content.loaders(loader)
    entries: List[Dict] = []
    seen = set(installed_ids)
    queue = [(project_id, 0)]
    versions: Dict[str, Dict] = {}
    while queue:
        pid, depth = queue.pop(0)
        if pid in seen and pid != project_id:
            continue
        seen.add(pid)
        version = get_latest_version(pid, game_version, loaders, None, channel.version_types)
        if not version:
            if pid == project_id:
                return []
            logging.warning(f"Для зависимости {pid} нет совместимой версии.")
            continue
        versions[pid] = version
        if depth < max_depth and content.has_dependencies:
            for dep in version.get('dependencies') or []:
                if dep.get('dependency_type') == 'required' and dep.get('project_id') and dep['project_id'] not in seen:
                    queue.append((dep['project_id'], depth + 1))
    projects = get_projects_info_from_ids(versions)
    for pid, version in versions.items():
        entry = {"name": pid, "project_id": pid, "current_version": "", "filepath": None,
                 "status": ModStatus.READY_TO_INSTALL, "installed_info": None}
        _apply_project_info(entry, projects.get(pid))
        if apply_install_info(entry, version):
            entries.append(entry)
    entries.sort(key=lambda e: e["project_id"] != project_id)
    return entries


# --- Список изменений между установленной и новой версией ---

def collect_changelogs(entries: List[Dict], game_version: str, loader: Optional[str],
                       content: ContentType = MODS) -> Dict[str, List[Dict]]:
    """
    project_id -> версии новее установленной (до предлагаемой включительно), новые первыми:
    [{version_number, version_type, date, changelog}]. Сетевые ошибки по отдельным модам пропускаются.
    """
    loaders = content.loaders(loader)

    def fetch(entry: Dict) -> List[Dict]:
        installed = (entry.get("installed_info") or {}).get("date_published") or ""
        latest = (entry.get("_latest_info") or {}).get("date_published") or "9999"
        result = []
        for v in get_all_versions(entry["project_id"], game_version, loaders):
            date = v.get("date_published") or ""
            if installed < date <= latest:
                result.append({"version_number": v.get("version_number"), "version_type": v.get("version_type"),
                               "date": format_date(date), "changelog": v.get("changelog") or ""})
        return result

    result: Dict[str, List[Dict]] = {}
    targets = [e for e in entries if e.get("project_id")]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch, e): e["project_id"] for e in targets}
        for future in concurrent.futures.as_completed(futures):
            try:
                result[futures[future]] = future.result()
            except Exception as e:
                logging.warning(f"Не удалось загрузить список изменений {futures[future]}: {e}")
    return result


# --- Переход на другую версию Minecraft ---

def check_target_version(entries: List[Dict], target_version: str, loader: Optional[str], channel: UpdateChannel,
                         content: ContentType = MODS) -> List[Dict]:
    """
    Для каждого установленного мода — есть ли версия под target_version.
    Возвращает [{entry, version}] (version = None, если версии нет или мод не с Modrinth).
    """
    with_hash = [e for e in entries if e.get("sha1") and e.get("project_id")]
    latest = get_latest_versions_from_hashes([e["sha1"] for e in with_hash], target_version,
                                             content.loaders(loader), channel.version_types) if with_hash else {}
    result = []
    for entry in entries:
        version = latest.get(entry.get("sha1")) if entry.get("sha1") else None
        if version and target_version not in (version.get("game_versions") or [target_version]):
            version = None
        result.append({"entry": entry, "version": version})
    return result
