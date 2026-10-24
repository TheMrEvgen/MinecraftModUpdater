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
from i18n import tr

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
        logging.warning(tr('Не удалось прочитать метаданные из {0}: {1}', filename, e))

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
        # Кэш могут одновременно писать окно и фоновая проверка в трее — ждём освобождения базы, а не падаем
        self.conn = sqlite3.connect(db_path, timeout=30)
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS mod_info (sha1 TEXT PRIMARY KEY, data TEXT);
            CREATE TABLE IF NOT EXISTS project_info (project_id TEXT PRIMARY KEY, data TEXT);
            CREATE TABLE IF NOT EXISTS cf_fingerprint (sha1 TEXT PRIMARY KEY, fingerprint INTEGER);
            CREATE TABLE IF NOT EXISTS file_index (
                path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER,
                sha1 TEXT, name TEXT, version TEXT, project_hint TEXT
            );
        ''')
        self.conn.commit()

    def close(self):
        self.conn.close()

    def get_fingerprints(self, sha1s: List[str]) -> Dict[str, int]:
        """Отпечатки CurseForge по SHA1: считать MurmurHash2 заново для неизменных файлов незачем."""
        result: Dict[str, int] = {}
        for chunk in (sha1s[i:i + 500] for i in range(0, len(sha1s), 500)):
            marks = ",".join("?" * len(chunk))
            for sha1, value in self.conn.execute(f"SELECT sha1, fingerprint FROM cf_fingerprint WHERE sha1 IN ({marks})", chunk):
                result[sha1] = int(value)
        return result

    def put_fingerprints(self, values: Dict[str, int]):
        if values:
            self.conn.executemany("INSERT OR REPLACE INTO cf_fingerprint (sha1, fingerprint) VALUES (?, ?)",
                                  list(values.items()))
            self.conn.commit()

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
                'categories', 'updated', 'downloads', 'followers', 'source_url', 'issues_url', 'wiki_url',
                'discord_url', 'donation_urls', 'loaders')

        def compact(info: Dict) -> Dict:
            data = {k: info.get(k) for k in keys}
            # Галерея: только адреса и подписи — полное описание проекта (body) в кэш не кладём
            data['gallery'] = [{'url': g.get('url'), 'raw_url': g.get('raw_url'), 'title': g.get('title'),
                                'featured': g.get('featured')} for g in (info.get('gallery') or [])][:12]
            return data

        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO project_info (project_id, data) VALUES (?, ?)",
                                  [(pid, json.dumps(compact(info))) for pid, info in infos.items()])

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
        logging.info(tr('Индексация {0} новых или изменённых файлов (из кэша: {1}).', len(to_index), len(result)))
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
                    logging.error(tr('Не удалось прочитать файл: {0}', e))
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
    entry["manual_url"] = version_info.get('manual_url') or ""  # CurseForge: скачивание только с сайта
    return True


# Загрузчики, моды для которых работают на данном (Quilt запускает моды Fabric)
COMPATIBLE_LOADERS = {"quilt": {"quilt", "fabric"}}


def dependency_applies(project: Optional[Dict], loader: Optional[str]) -> bool:
    """
    Нужна ли зависимость на этом загрузчике. Мультизагрузочные версии (одна версия для Fabric и NeoForge)
    перечисляют зависимости всех загрузчиков сразу — например, Fabric API у мода, который стоит на NeoForge.
    Если проект зависимости не выпускается для текущего загрузчика, зависимость относится к другому загрузчику.
    """
    project_loaders = set((project or {}).get('loaders') or [])
    if not loader or not project_loaders:
        return True  # Нет данных — не скрываем
    return bool(project_loaders & COMPATIBLE_LOADERS.get(loader, {loader}))


def project_links(project: Dict) -> List[tuple]:
    """Ссылки проекта для панели мода: (подпись, иконка, адрес)."""
    links = []
    for key, title, icon in (("source_url", tr("Исходный код"), "code"), ("issues_url", tr("Сообщить об ошибке"), "bug"),
                             ("wiki_url", tr("Вики"), "book"), ("discord_url", "Discord", "chat")):
        if project.get(key):
            links.append((title, icon, project[key]))
    for donation in project.get('donation_urls') or []:
        if donation.get('url'):
            links.append((tr("Поддержать автора"), "heart", donation['url']))
            break
    return links


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
    entry["project_updated"] = project.get('updated')
    entry["downloads"] = project.get('downloads')
    entry["links"] = project_links(project)
    entry["gallery"] = list(project.get('gallery') or [])
    if project.get('page_url'):  # Мод с CurseForge: своя страница и источник
        entry["page_url"] = project['page_url']
        entry["source"] = project.get('source')


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
        progress(tr("Индексация локальных файлов..."))
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
            progress(tr('Запрос информации о {0} модах с Modrinth...', len(to_fetch)))
            fetched = get_mods_info_from_hashes(to_fetch)
            if cache and fetched:
                cache.put_mod_infos(fetched)
            installed_infos.update(fetched)
        check_cancel()

        if not installed_infos:
            progress(tr("Не удалось получить информацию о модах. Проверьте интернет-соединение."))

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
            progress(tr('Проверка обновлений для {0} модов...', len(installed)))
            try:
                latest_by_hash = get_latest_versions_from_hashes(
                    [d['sha1'] for d in installed.values()], game_version, loaders, channel.version_types)
            except requests.RequestException as e:
                logging.error(tr('Не удалось получить последние версии: {0}', e))
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
            progress(tr('Поиск {0} недостающих зависимостей...', len(missing_ids)))
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
                        logging.error(tr('Не удалось получить версию для зависимости {0}: {1}', pid, e))
                        latest_by_project[pid] = None

        # --- Информация о проектах: названия, иконки, стороны (клиент/сервер) ---
        projects_info: Dict[str, Dict] = {}
        all_project_ids = set(installed) | dependency_ids | set(duplicates)
        if all_project_ids:
            progress(tr('Запрос информации о {0} проектах...', len(all_project_ids)))
            try:
                projects_info = get_projects_info_from_ids(all_project_ids)
                if cache and projects_info:
                    cache.put_project_infos(projects_info)
            except requests.RequestException as e:
                logging.error(tr('Не удалось получить информацию о проектах: {0}', e))
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
                logging.info(tr("Найдено обновление для '{0}': {1} -> {2}.", entry['name'], entry['current_version'], entry['new_version']))
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
                if content.has_dependencies and not is_installed and not dependency_applies(project, loader):
                    logging.debug(f"Зависимость {project.get('title', dep_pid)} мода {project_id} относится к другому "
                                  f"загрузчику ({', '.join(project.get('loaders') or [])}) — пропущена.")
                    continue
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

        # --- Моды, не найденные на Modrinth: спрашиваем CurseForge ---
        unknown = [local for local in local_files
                   if not (installed_infos.get(local['sha1']) or {}).get('project_id')]
        check_cancel()
        from_curseforge = _curseforge_entries(unknown, game_version, loader if content is MODS else None, channel,
                                              cache, progress, check_cancel, installed_keys(mods_data))
        for local in unknown:
            entry = from_curseforge.get(local['path'])
            if entry is None:
                entry = make_local_entry(local, ModStatus.NOT_FOUND)
                entry["project_id"] = None
            mods_data.append(entry)

        return mods_data
    finally:
        if cache:
            cache.close()


def _curseforge_entries(locals_: List[Dict], game_version: str, loader: Optional[str], channel: UpdateChannel,
                        cache: Optional["ModCache"], progress: ProgressCallback = lambda msg: None,
                        check_cancel: Callable[[], None] = lambda: None, installed: Optional[set] = None) -> Dict[str, Dict]:
    """
    Файлы, которых нет на Modrinth, — по отпечатку на CurseForge. Путь → запись мода (как у Modrinth):
    установленная версия, страница, иконка, последняя подходящая версия и недостающие обязательные зависимости.
    installed — ключи уже установленных модов (installed_keys). Без ключа API — пусто.
    """
    import curseforge
    if not locals_ or not curseforge.available():
        return {}
    progress(tr("Ищем {0} модов на CurseForge…", len(locals_)))
    known = cache.get_fingerprints([l['sha1'] for l in locals_]) if cache else {}
    fresh = {}
    prints: Dict[int, Dict] = {}
    for local in locals_:
        value = known.get(local['sha1'])
        if value is None:
            try:
                value = curseforge.file_fingerprint(local['path'])
            except OSError:
                continue
            fresh[local['sha1']] = value
        prints[value] = local
    if cache:
        cache.put_fingerprints(fresh)
    try:
        matches = curseforge.match_fingerprints(prints)
        mods = curseforge.get_mods({m["mod_id"] for m in matches.values()})
    except requests.RequestException as e:
        logging.warning(tr("CurseForge недоступен: {0}", e))
        return {}
    check_cancel()

    def build(item):
        value, match = item
        mod = mods.get(int(match["mod_id"] or 0))
        if not mod or mod.get("isAvailable") is False:  # Удалённые проекты (перезаливы чужих модов) не считаем
            return None
        local, file = prints[value], match["file"]
        installed = curseforge.to_version(file, mod)
        entry = make_local_entry(local, ModStatus.UP_TO_DATE)
        entry["project_id"] = curseforge.project_id(mod["id"])
        entry["current_version"] = installed["version_number"]
        entry["installed_info"] = installed
        _apply_project_info(entry, curseforge.to_project(mod))
        try:
            files = curseforge.files_for(mod["id"], game_version, loader) if game_version else []
        except requests.RequestException as e:
            logging.warning(tr("CurseForge: версии «{0}» не получены: {1}", entry["name"], e))
            files = []
        newest = curseforge.latest_file(files, game_version, loader or "", channel.value)
        if newest and newest.get("id") != file.get("id") and (newest.get("fileDate") or "") > (file.get("fileDate") or ""):
            latest = curseforge.to_version(newest, mod)
            if apply_install_info(entry, latest):
                entry["status"] = ModStatus.UPDATE_AVAILABLE
                entry["manual_url"] = latest["manual_url"]  # Автор запретил скачивание программами — вручную
                logging.info(tr("Найдено обновление для '{0}': {1} -> {2}.", entry['name'], entry['current_version'], entry['new_version']))
        return local['path'], entry

    result = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        for built in executor.map(build, matches.items()):
            if built:
                result[built[0]] = built[1]
    logging.info(tr("CurseForge: узнано {0} из {1} модов, которых нет на Modrinth.", len(result), len(locals_)))
    if loader and result:  # У шейдеров и ресурспаков зависимостей нет
        _attach_curseforge_dependencies(result, set(installed or ()), game_version, loader, channel)
    return result


def _key(text: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def installed_keys(entries: List[Dict]) -> set:
    """Адреса и названия установленных модов — зависимость считается выполненной, из какого бы каталога ни стояла."""
    keys = set()
    for entry in entries:
        for item in [entry] + list(entry.get("dependencies") or []):
            if item.get("filepath"):
                keys.update(k for k in (_key(item.get("slug")), _key(item.get("name"))) if k)
    return keys


def _attach_curseforge_dependencies(result: Dict[str, Dict], installed: set, game_version: str, loader: str,
                                    channel: UpdateChannel):
    """Обязательные зависимости модов с CurseForge, которых нет в сборке (с поиском версии под сборку)."""
    import curseforge
    installed = installed | {k for e in result.values() for k in (_key(e.get("slug")), _key(e.get("name"))) if k}
    wanted: Dict[int, List[Dict]] = {}
    for entry in result.values():
        for dep in (entry.get("installed_info") or {}).get("dependencies") or []:
            dep_id = curseforge.mod_id(dep.get("project_id"))
            if dep.get("dependency_type") == "required" and dep_id:
                wanted.setdefault(dep_id, []).append(entry)
    if not wanted:
        return
    try:
        dep_mods = curseforge.get_mods(wanted)
    except requests.RequestException as e:
        logging.warning(tr("CurseForge недоступен: {0}", e))
        return
    for dep_id, parents in wanted.items():
        mod = dep_mods.get(dep_id)
        if not mod or mod.get("isAvailable") is False:
            continue
        project = curseforge.to_project(mod)
        if {_key(mod.get("slug")), _key(mod.get("name"))} & installed or not dependency_applies(project, loader):
            continue  # Уже стоит (в том числе с Modrinth) или нужна другому загрузчику
        try:
            newest = curseforge.latest_file(curseforge.files_for(dep_id, game_version, loader), game_version, loader,
                                            channel.value)
        except requests.RequestException:
            newest = None
        dep_entry = {"name": project["title"], "project_id": project["id"], "current_version": "", "filepath": None,
                     "status": ModStatus.REQUIRED, "new_version": "", "new_version_date": "", "download_url": None,
                     "new_filename": None, "new_hashes": None, "installed_info": None, "type": "required",
                     "installed": False}
        _apply_project_info(dep_entry, project)
        if newest:
            apply_install_info(dep_entry, curseforge.to_version(newest, mod))
        for parent in parents:
            parent.setdefault("dependencies", []).append(dict(dep_entry))


# --- Импорт .mrpack ---

def read_mrpack(path: str) -> Dict:
    """Читает modrinth.index.json из .mrpack. Выбрасывает ValueError при некорректном файле."""
    try:
        with zipfile.ZipFile(path) as z:
            index = json.loads(z.read("modrinth.index.json").decode("utf-8"))
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(tr('Не удалось прочитать .mrpack: {0}', e)) from e
    if not isinstance(index, dict) or not isinstance(index.get("files"), list):
        raise ValueError(tr("Некорректный modrinth.index.json"))
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
        logging.warning(tr('Не удалось получить информацию о модах сборки: {0}', e))
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
                            installed_ids=(), content: ContentType = MODS, max_depth: int = 3,
                            root_version: Optional[Dict] = None) -> List[Dict]:
    """
    Записи «к установке» для проекта из каталога и его обязательных зависимостей, которых ещё нет в сборке.
    Возвращает пустой список, если совместимой версии нет. Первая запись — сам проект.
    root_version — конкретная версия проекта (выбранная в списке версий) вместо последней.
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
        if pid == project_id and root_version:
            version = root_version
        else:
            version = get_latest_version(pid, game_version, loaders, None, channel.version_types)
        if not version:
            if pid == project_id:
                return []
            logging.warning(tr('Для зависимости {0} нет совместимой версии.', pid))
            continue
        versions[pid] = version
        if depth < max_depth and content.has_dependencies:
            dep_ids = [d['project_id'] for d in version.get('dependencies') or []
                       if d.get('dependency_type') == 'required' and d.get('project_id') and d['project_id'] not in seen]
            dep_projects = get_projects_info_from_ids(dep_ids) if dep_ids and loader else {}
            for dep_id in dep_ids:
                if dependency_applies(dep_projects.get(dep_id), loader):
                    queue.append((dep_id, depth + 1))
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
                changelog = v.get("changelog") or ""
                if not changelog and v.get("cf_file_id") and len(result) < 8:  # У CurseForge — отдельный запрос
                    import curseforge
                    changelog = curseforge.file_changelog(entry["project_id"], v["cf_file_id"])
                result.append({"version_number": v.get("version_number"), "version_type": v.get("version_type"),
                               "date": format_date(date), "changelog": changelog})
        return result

    result: Dict[str, List[Dict]] = {}
    targets = [e for e in entries if e.get("project_id")]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch, e): e["project_id"] for e in targets}
        for future in concurrent.futures.as_completed(futures):
            try:
                result[futures[future]] = future.result()
            except Exception as e:
                logging.warning(tr('Не удалось загрузить список изменений {0}: {1}', futures[future], e))
    return result


# --- Переход на другую версию Minecraft ---

def check_target_version(entries: List[Dict], target_version: str, loader: Optional[str], channel: UpdateChannel,
                         content: ContentType = MODS) -> List[Dict]:
    """
    Для каждого установленного мода — есть ли версия под target_version.
    Возвращает [{entry, version}] (version = None, если версии нет или мод не опознан ни на Modrinth, ни на CurseForge).
    """
    with_hash = [e for e in entries if e.get("sha1") and e.get("project_id") and not str(e["project_id"]).startswith("cf:")]
    latest = get_latest_versions_from_hashes([e["sha1"] for e in with_hash], target_version,
                                             content.loaders(loader), channel.version_types) if with_hash else {}
    result = []
    for entry in entries:
        if str(entry.get("project_id") or "").startswith("cf:"):  # Мод с CurseForge: Modrinth его не знает
            try:
                version = get_latest_version(entry["project_id"], target_version, content.loaders(loader), None,
                                             channel.version_types)
            except requests.RequestException as e:
                logging.warning(tr("CurseForge: версии «{0}» не получены: {1}", entry.get("name"), e))
                version = None
            result.append({"entry": entry, "version": version})
            continue
        version = latest.get(entry.get("sha1")) if entry.get("sha1") else None
        if version and target_version not in (version.get("game_versions") or [target_version]):
            version = None
        result.append({"entry": entry, "version": version})
    return result


def resolve_many_installs(project_ids: List[str], game_version: str, loader: Optional[str], channel: UpdateChannel,
                          installed_ids=(), content: ContentType = MODS) -> tuple[List[Dict], List[str]]:
    """Установка сразу нескольких проектов: общие зависимости не дублируются. Возвращает (записи, без версии)."""
    entries: Dict[str, Dict] = {}
    missing = []
    for pid in project_ids:
        if pid in entries:
            continue
        found = resolve_project_install(pid, game_version, loader, channel, set(installed_ids) | set(entries), content)
        if not found:
            missing.append(pid)
        for entry in found:
            entries.setdefault(entry["project_id"], entry)
    return list(entries.values()), missing
