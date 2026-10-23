"""Фоновые потоки Qt. Вся логика находится в scanner.py / updater.py, здесь только обвязка сигналами."""
import concurrent.futures
import logging
import os
import threading
import time
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, QRunnable, QThread, QThreadPool, Qt, pyqtSignal
from PyQt6.QtGui import QImage
from packaging.version import InvalidVersion, parse as parse_version

from content import MODS, ContentType
from enums import ModStatus, UpdateChannel
from mod_updater_logic import (
    SESSION, download_bytes, get_all_versions, get_latest_loader_version, get_latest_version,
    get_projects_info_from_ids,
)
from scanner import ScanCancelled, apply_install_info, scan_local, scan_mods
from updater import UpdateTransaction, install_mod
from i18n import tr


class ScanWorker(QThread):
    """Сканирование модов и проверка обновлений. finished(None) означает отмену."""
    finished = pyqtSignal(object)
    progress = pyqtSignal(str)

    def __init__(self, mods_path, game_version, loader, cache_file, channel: UpdateChannel, content: ContentType = MODS):
        super().__init__()
        self.content = content
        self.mods_path = mods_path
        self.game_version = game_version
        self.loader = loader
        self.cache_file = cache_file
        self.channel = channel
        self.is_running = True

    def run(self):
        try:
            mods_data = scan_mods(self.mods_path, self.game_version, self.loader, self.cache_file, self.channel,
                                  progress=self.progress.emit, is_cancelled=lambda: not self.is_running,
                                  content=self.content)
            self.finished.emit(mods_data)
        except ScanCancelled:
            logging.info(tr("Сканирование прервано пользователем."))
            self.finished.emit(None)
        except Exception as e:
            logging.exception(tr('Ошибка во время сканирования: {0}', e))
            self.progress.emit(tr('Ошибка сканирования: {0}', e))
            self.finished.emit([])

    def stop(self):
        self.is_running = False
        logging.info(tr("Получен запрос на остановку сканирования."))


class LocalScanWorker(QThread):
    """Быстрое локальное сканирование без сети. finished(None) означает отмену."""
    finished = pyqtSignal(object)

    def __init__(self, mods_path, cache_file, content: ContentType = MODS):
        super().__init__()
        self.content = content
        self.mods_path = mods_path
        self.cache_file = cache_file
        self.is_running = True

    def run(self):
        try:
            mods_data = scan_local(self.mods_path, self.cache_file, self.content)
            logging.info(tr('Локальное сканирование завершено. Найдено файлов: {0}.', len(mods_data)))
            self.finished.emit(mods_data if self.is_running else None)
        except Exception as e:
            logging.exception(tr('Ошибка во время локального сканирования: {0}', e))
            self.finished.emit([])

    def stop(self):
        self.is_running = False


class UpdateWorker(QThread):
    """Скачивание и установка модов. Все изменения записываются в транзакцию для отката."""
    finished = pyqtSignal()
    progress = pyqtSignal(int, str)  # индекс мода, статус
    download_progress = pyqtSignal(int, int, int)  # индекс мода, скачано, всего

    # Ограничиваем количество одновременных скачиваний, чтобы не вызывать защиту сервера
    MAX_WORKERS = 3
    # Не чаще одного обновления прогресса на мод за этот интервал (сек.), чтобы не перегружать UI
    PROGRESS_INTERVAL = 0.1

    def __init__(self, mods_to_update: List[Dict], mods_path: str, transaction: Optional[UpdateTransaction] = None,
                 max_workers: int = MAX_WORKERS):
        super().__init__()
        self.max_workers = max(1, int(max_workers))
        self.mods_to_update = mods_to_update
        self.mods_path = mods_path
        self.transaction = transaction or UpdateTransaction(mods_path, "update")
        self.results: Dict[int, ModStatus] = {}
        self.is_running = True
        self._results_lock = threading.Lock()

    def stop(self):
        self.is_running = False
        logging.info(tr("Получен запрос на остановку обновления."))

    def _process_single_mod(self, i: int, mod: Dict):
        if not self.is_running:
            status = ModStatus.CANCELLED
        else:
            self.progress.emit(i, ModStatus.DOWNLOADING.value)
            last_emit = [0.0]

            def callback(downloaded, total):
                now = time.monotonic()
                if now - last_emit[0] >= self.PROGRESS_INTERVAL or downloaded >= total:
                    last_emit[0] = now
                    self.download_progress.emit(i, downloaded, total)

            try:
                status = install_mod(mod, self.mods_path, self.transaction, callback, lambda: not self.is_running)
            except Exception as e:
                logging.critical(tr('Неперехваченная ошибка при обработке мода {0}: {1}', mod.get('name'), e))
                status = ModStatus.CRITICAL_ERROR
        with self._results_lock:
            self.results[i] = status
        self.progress.emit(i, status.value)

    def run(self):
        logging.info(tr('Запуск обновления {0} модов в {1} потока.', len(self.mods_to_update), self.max_workers))
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = [executor.submit(self._process_single_mod, i, mod) for i, mod in enumerate(self.mods_to_update)]
            concurrent.futures.wait(futures)
        logging.info(tr("Все задачи по обновлению модов завершены."))
        self.finished.emit()


class ImportWorker(QThread):
    """Получение информации о модах из импортированного списка. finished(None) означает отмену."""
    finished = pyqtSignal(object)
    progress = pyqtSignal(str)

    def __init__(self, target_mods, game_version, loader, use_exact_versions=False,
                 channel: UpdateChannel = UpdateChannel.RELEASE):
        super().__init__()
        self.target_mods = target_mods
        self.project_ids = list(target_mods.keys()) if isinstance(target_mods, dict) else list(target_mods)
        self.game_version = game_version
        self.loader = loader
        self.use_exact_versions = use_exact_versions
        self.channel = channel
        self.is_running = True

    def stop(self):
        self.is_running = False
        logging.info(tr("Получен запрос на остановку импорта."))

    def run(self):
        try:
            self.finished.emit(self._run())
        except Exception as e:
            logging.exception(tr('Ошибка импорта: {0}', e))
            self.progress.emit(tr('Ошибка импорта: {0}', e))
            self.finished.emit([])

    def _run(self):
        self.progress.emit(tr('Запрос информации о {0} проектах...', len(self.project_ids)))
        projects_info = get_projects_info_from_ids(self.project_ids)
        if not projects_info:
            self.progress.emit(tr("Не удалось получить информацию о проектах."))
            return []

        self.progress.emit(tr('Поиск совместимых версий для {0} модов...', len(projects_info)))
        versions_map = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = {}
            for project_id in self.project_ids:
                if project_id not in projects_info:
                    continue
                target_version = None
                if self.use_exact_versions and isinstance(self.target_mods, dict):
                    target_version = self.target_mods.get(project_id)
                futures[executor.submit(get_latest_version, project_id, self.game_version, self.loader,
                                        target_version, self.channel.version_types)] = project_id
            for done, future in enumerate(concurrent.futures.as_completed(futures), start=1):
                if not self.is_running:
                    executor.shutdown(wait=False, cancel_futures=True)
                    return None
                project_id = futures[future]
                self.progress.emit(tr('Поиск версий: {0} / {1}', done, len(futures)))
                try:
                    versions_map[project_id] = future.result()
                except Exception as e:
                    logging.error(tr('Не удалось получить версию для импорта проекта {0}: {1}', project_id, e))

        mods_data = []
        for project_id in self.project_ids:
            project = projects_info.get(project_id)
            if not project:
                logging.warning(tr('Не удалось найти информацию для project_id: {0}', project_id))
                continue
            entry = {
                "name": project.get('title') or project.get('slug', project_id), "project_id": project_id,
                "current_version": "", "filepath": None, "status": ModStatus.READY_TO_INSTALL,
                "icon_url": project.get('icon_url'), "client_side": project.get('client_side'),
                "server_side": project.get('server_side'), "slug": project.get('slug'),
            }
            if apply_install_info(entry, versions_map.get(project_id)):
                mods_data.append(entry)
            else:
                logging.warning(tr("Не найдено совместимых версий для '{0}' (ID: {1}).", entry['name'], project_id))
        return mods_data


class LoaderVersionWorker(QThread):
    """Проверка версии загрузчика."""
    finished = pyqtSignal(str, str, str)  # сообщение, ключ цвета ('ok', 'update', 'error'), последняя версия

    def __init__(self, loader, game_version, current_loader_version):
        super().__init__()
        self.loader = loader
        self.game_version = game_version
        self.current_loader_version = current_loader_version

    def run(self):
        latest = get_latest_loader_version(self.loader, self.game_version)
        if not latest:
            self.finished.emit(f"⚠️ ({self.current_loader_version})", 'error', "")
            return
        try:
            if parse_version(latest) > parse_version(self.current_loader_version):
                logging.info(tr('Доступно обновление загрузчика {0}: {1} -> {2}', self.loader, self.current_loader_version, latest))
                self.finished.emit(f"🔼 ({self.current_loader_version} -> {latest})", 'update', latest)
            else:
                logging.info(tr('Загрузчик {0} актуален (версия {1}).', self.loader, self.current_loader_version))
                self.finished.emit(f"✅ ({self.current_loader_version})", 'ok', latest)
        except InvalidVersion as e:
            logging.error(tr('Ошибка сравнения версий загрузчика: {0}', e))
            self.finished.emit(f"⚠️ ({self.current_loader_version})", 'error', latest)


class TaskWorker(QThread):
    """Универсальный поток для одиночной блокирующей операции (сетевой запрос и т.п.)."""
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self._func = func
        self._args = args
        self._kwargs = kwargs

    def run(self):
        try:
            self.succeeded.emit(self._func(*self._args, **self._kwargs))
        except Exception as e:
            logging.error(tr('Ошибка фоновой операции: {0}', e))
            self.failed.emit(str(e))


def fetch_json(url: str):
    response = SESSION.get(url, timeout=15)
    response.raise_for_status()
    return response.json()


def fetch_versions(project_id: str, game_version: str, loader: str):
    return get_all_versions(project_id, game_version, loader)


# --- Иконки модов ---

class _IconSignals(QObject):
    loaded = pyqtSignal(str, str)  # project_id, путь к файлу


class _IconTask(QRunnable):
    def __init__(self, project_id: str, url: str, path: str, size: int, signals: _IconSignals):
        super().__init__()
        self.project_id = project_id
        self.url = url
        self.path = path
        self.size = size
        self.signals = signals

    def run(self):
        try:
            image = QImage()
            if not image.loadFromData(download_bytes(self.url)):
                return
            image = image.scaled(self.size, self.size, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            tmp_path = self.path + ".tmp.png"
            if image.save(tmp_path, "PNG"):
                os.replace(tmp_path, self.path)
                self.signals.loaded.emit(self.project_id, self.path)
        except Exception as e:  # Иконка не критична: любая ошибка просто оставляет строку без неё
            logging.debug(f"Не удалось загрузить иконку {self.project_id}: {e}")


class IconLoader(QObject):
    """
    Асинхронная загрузка иконок модов с дисковым кэшем (иконки хранятся уменьшенными в PNG).
    Сигнал icon_ready(project_id, path) приходит в главном потоке.
    """
    icon_ready = pyqtSignal(str, str)
    ICON_SIZE = 96

    def __init__(self, cache_dir: str, parent=None, size: int = ICON_SIZE):
        super().__init__(parent)
        self.size = size
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(4)
        self._pending: set[str] = set()
        self._signals = _IconSignals()
        self._signals.loaded.connect(self._on_loaded)

    def cached_path(self, project_id: str) -> Optional[str]:
        path = os.path.join(self.cache_dir, f"{project_id}.png")
        return path if os.path.exists(path) else None

    def request(self, project_id: Optional[str], url: Optional[str]) -> Optional[str]:
        """Возвращает путь, если иконка уже в кэше; иначе ставит загрузку в очередь и возвращает None."""
        if not project_id:
            return None
        cached = self.cached_path(project_id)
        if cached:
            return cached
        if url and project_id not in self._pending:
            self._pending.add(project_id)
            path = os.path.join(self.cache_dir, f"{project_id}.png")
            self.pool.start(_IconTask(project_id, url, path, self.size, self._signals))
        return None

    def request_url(self, url: Optional[str]) -> Optional[str]:
        """Картинка по адресу (скриншоты галереи): ключ кэша — хэш адреса. Сигнал icon_ready(ключ, путь)."""
        return self.request(self.url_key(url), url) if url else None

    @staticmethod
    def url_key(url: str) -> str:
        import hashlib
        return hashlib.sha1(url.encode("utf-8")).hexdigest()

    def _on_loaded(self, project_id: str, path: str):
        self._pending.discard(project_id)
        self.icon_ready.emit(project_id, path)

    def shutdown(self, wait_ms: int = 3000):
        self.pool.clear()
        if wait_ms:
            self.pool.waitForDone(wait_ms)
