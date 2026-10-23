"""
Установка модов, резервные копии и откат (без зависимостей от Qt).

Каждая операция, меняющая папку mods (обновление, установка, удаление, «заменить все»), выполняется
в рамках UpdateTransaction: заменяемые файлы переносятся в отдельную папку бэкапа с журналом
manifest.json. По журналу последнюю операцию можно полностью откатить.
"""
import datetime
import json
import logging
import os
import shutil
import sys
import threading
from typing import Callable, Dict, Iterable, List, Optional

from content import DISABLED_SUFFIX, MODS, ContentType, enabled_name, is_disabled
from enums import ModStatus
from mod_updater_logic import (
    DownloadCancelled, DownloadError, HashMismatchError, download_file, safe_filename,
)
from i18n import tr

BACKUP_DIR_NAME = "ModUpdater_Backups"
TRANSACTIONS_DIR_NAME = "transactions"
SNAPSHOTS_DIR_NAME = "snapshots"
MANIFEST_NAME = "manifest.json"

KIND_LABELS = {
    "update": tr("обновление/установка"),
    "delete": tr("удаление"),
    "replace_all": tr("замена сборки"),
    "add": tr("добавление файлов"),
    "restore": tr("восстановление снимка"),
    "migrate": tr("переход на другую версию игры"),
}

SNAPSHOT_REASONS = {
    "manual": tr("создан вручную"),
    "update_all": tr("перед обновлением всех модов"),
    "replace_all": tr("перед заменой сборки"),
    "migrate": tr("перед переходом на другую версию игры"),
    "restore": tr("перед восстановлением снимка"),
}


def make_unique_dir(path: str) -> str:
    """
    Создаёт новую папку path (или path_2, path_3…, если имя занято) и возвращает её путь.
    Часы Windows идут шагами ~15 мс, поэтому две операции подряд могут получить одинаковую метку времени.
    """
    candidate, counter = path, 2
    while True:
        try:
            os.makedirs(candidate)
            return candidate
        except FileExistsError:
            candidate = f"{path}_{counter}"
            counter += 1


def backup_root(mods_path: str) -> str:
    return os.path.join(mods_path, BACKUP_DIR_NAME)


def transactions_root(mods_path: str) -> str:
    return os.path.join(backup_root(mods_path), TRANSACTIONS_DIR_NAME)


class UpdateTransaction:
    """Журнал одной операции над папкой mods. Потокобезопасен (обновления идут параллельно)."""

    def __init__(self, mods_path: str, kind: str = "update"):
        self.mods_path = mods_path
        self.kind = kind
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
        self.dir = os.path.join(transactions_root(mods_path), timestamp)
        self.manifest = {"created": datetime.datetime.now().isoformat(timespec="seconds"), "kind": kind,
                         "installed": [], "backed_up": [], "rolled_back": False}
        self._lock = threading.Lock()
        self._dir_created = False

    @property
    def is_empty(self) -> bool:
        return not self.manifest["installed"] and not self.manifest["backed_up"]

    def _ensure_dir(self):
        if not self._dir_created:
            self.dir = make_unique_dir(self.dir)
            self._dir_created = True

    def backup_file(self, path: str) -> str:
        """Переносит файл или папку из mods в бэкап транзакции. Возвращает путь копии."""
        with self._lock:
            self._ensure_dir()
            name = os.path.basename(path)
            backup_name = name
            counter = 1
            while os.path.exists(os.path.join(self.dir, backup_name)):
                backup_name = f"{counter}_{name}"
                counter += 1
            backup_path = os.path.join(self.dir, backup_name)
            shutil.move(path, backup_path)
            self.manifest["backed_up"].append({"name": name, "backup": backup_name})
            self._save_locked()
        logging.info(tr('Файл {0} перемещен в бэкап: {1}', name, self.dir))
        return backup_path

    def restore_file(self, backup_path: str, original_path: str):
        """Возвращает файл из бэкапа (используется при сбое установки одного мода)."""
        with self._lock:
            shutil.move(backup_path, original_path)
            backup_name = os.path.basename(backup_path)
            self.manifest["backed_up"] = [e for e in self.manifest["backed_up"] if e["backup"] != backup_name]
            self._save_locked()

    def record_installed(self, filename: str):
        with self._lock:
            self._ensure_dir()
            self.manifest["installed"].append(filename)
            self._save_locked()

    def _save_locked(self):
        tmp_path = os.path.join(self.dir, MANIFEST_NAME + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.manifest, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, os.path.join(self.dir, MANIFEST_NAME))


# --- Откат ---

def list_transactions(mods_path: str) -> List[Dict]:
    """Список операций (новые первыми), которые ещё можно откатить."""
    root = transactions_root(mods_path)
    if not os.path.isdir(root):
        return []
    result = []
    for name in sorted(os.listdir(root), reverse=True):
        manifest_path = os.path.join(root, name, MANIFEST_NAME)
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("rolled_back") or not (manifest.get("installed") or manifest.get("backed_up")):
            continue
        manifest["dir"] = os.path.join(root, name)
        result.append(manifest)
    return result


def get_last_transaction(mods_path: str) -> Optional[Dict]:
    transactions = list_transactions(mods_path)
    return transactions[0] if transactions else None


def describe_transaction(manifest: Dict) -> str:
    created = manifest.get("created", "").replace("T", " ")
    kind = KIND_LABELS.get(manifest.get("kind"), manifest.get("kind", ""))
    return (tr('Операция: {0}\nДата: {1}\nУстановлено файлов: {2}\nФайлов в бэкапе: {3}', kind, created, len(manifest.get('installed', [])), len(manifest.get('backed_up', []))))


def rollback_transaction(mods_path: str, manifest: Dict) -> List[str]:
    """
    Откатывает операцию: удаляет установленные ею файлы и возвращает файлы из бэкапа.
    Возвращает список ошибок (пустой при успехе).
    """
    errors = []
    tx_dir = manifest["dir"]

    for filename in manifest.get("installed", []):
        path = os.path.join(mods_path, safe_filename(filename))
        try:
            if os.path.exists(path):
                os.remove(path)
                logging.info(tr('Откат: удален установленный файл {0}', filename))
        except OSError as e:
            errors.append(tr('Не удалось удалить {0}: {1}', filename, e))

    for entry in manifest.get("backed_up", []):
        source = os.path.join(tx_dir, entry["backup"])
        target = os.path.join(mods_path, entry["name"])
        if not os.path.exists(source):
            errors.append(tr('Бэкап {0} не найден', entry['backup']))
            continue
        if os.path.exists(target):
            errors.append(tr('Файл {0} уже существует в папке mods, восстановление пропущено', entry['name']))
            continue
        try:
            shutil.move(source, target)
            logging.info(tr('Откат: восстановлен файл {0}', entry['name']))
        except OSError as e:
            errors.append(tr('Не удалось восстановить {0}: {1}', entry['name'], e))

    manifest_path = os.path.join(tx_dir, MANIFEST_NAME)
    if errors:
        # Оставляем бэкап на месте, но помечаем операцию, чтобы не откатывать её повторно
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                stored = json.load(f)
            stored["rolled_back"] = True
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(stored, f, ensure_ascii=False, indent=2)
        except (OSError, json.JSONDecodeError) as e:
            errors.append(tr('Не удалось обновить журнал операции: {0}', e))
    else:
        shutil.rmtree(tx_dir, ignore_errors=True)
    return errors


# --- Проверка блокировки файлов (запущенный Minecraft) ---

def _is_file_locked_windows(path: str) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    GENERIC_READ = 0x80000000
    OPEN_EXISTING = 3
    ERROR_SHARING_VIOLATION = 32
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    # Открытие без общего доступа (share mode = 0) не удаётся, если файл открыт другим процессом.
    # Файл при этом не изменяется.
    handle = kernel32.CreateFileW(path, GENERIC_READ, 0, None, OPEN_EXISTING, 0x80, None)
    if handle == INVALID_HANDLE_VALUE:
        return ctypes.get_last_error() == ERROR_SHARING_VIOLATION
    kernel32.CloseHandle(handle)
    return False


def find_locked_files(paths: Iterable[str]) -> List[str]:
    """Файлы, открытые другим процессом (обычно запущенной игрой). Работает только на Windows."""
    if sys.platform != "win32":
        return []
    locked = []
    for path in paths:
        try:
            if path and os.path.isfile(path) and _is_file_locked_windows(path):
                locked.append(path)
        except OSError:
            continue
    return locked


def set_disabled(path: str, disabled: bool) -> str:
    """Включает или выключает файл переименованием (*.disabled). Возвращает новый путь."""
    if is_disabled(path) == disabled:
        return path
    folder, name = os.path.split(path)
    target = os.path.join(folder, name + DISABLED_SUFFIX if disabled else enabled_name(name))
    if os.path.exists(target):
        raise FileExistsError(tr('Файл {0} уже существует', os.path.basename(target)))
    os.rename(path, target)
    logging.info(tr('Файл {0} {1}.', name, tr('выключен') if disabled else tr('включён')))
    return target


def is_game_running(mods_path: str, sample_size: int = 5) -> bool:
    """Эвристика: если игра запущена, загруженные jar-файлы модов заблокированы."""
    if sys.platform != "win32" or not os.path.isdir(mods_path):
        return False
    jars = [os.path.join(mods_path, f) for f in os.listdir(mods_path) if f.lower().endswith('.jar')]
    return bool(find_locked_files(jars[:sample_size]))


# --- Установка ---

def install_mod(mod: Dict, mods_path: str, transaction: UpdateTransaction,
                progress_callback: Optional[Callable[[int, int], None]] = None,
                is_cancelled: Optional[Callable[[], bool]] = None) -> ModStatus:
    """
    Скачивает и устанавливает один мод. Старый файл не трогается, пока новый не скачан
    и не проверен по хэшу; при ошибке замены старые файлы возвращаются на место.
    """
    mod_name = mod.get('name', '?')
    try:
        new_filename = safe_filename(mod['new_filename'])
    except (KeyError, ValueError) as e:
        logging.error(tr("Некорректное имя файла для '{0}': {1}", mod_name, e))
        return ModStatus.DOWNLOAD_ERROR

    if mod.get('disabled'):
        new_filename += DISABLED_SUFFIX  # Обновление выключенного мода не включает его
    final_path = os.path.join(mods_path, new_filename)
    temp_path = final_path + ".part"

    try:
        download_file(mod['download_url'], temp_path, expected_hashes=mod.get('new_hashes'),
                      progress_callback=progress_callback, is_cancelled=is_cancelled)
    except DownloadCancelled:
        return ModStatus.CANCELLED
    except HashMismatchError as e:
        logging.error(f"'{mod_name}': {e}")
        return ModStatus.HASH_ERROR
    except DownloadError as e:
        logging.error(tr("Не удалось скачать мод '{0}': {1}", mod_name, e))
        return ModStatus.DOWNLOAD_ERROR

    backups: List[tuple] = []  # (backup_path, original_path)
    try:
        old_path = mod.get('filepath')
        if old_path and os.path.exists(old_path) and os.path.normcase(os.path.normpath(old_path)) != os.path.normcase(os.path.normpath(final_path)):
            backups.append((transaction.backup_file(old_path), old_path))
        if os.path.exists(final_path):
            backups.append((transaction.backup_file(final_path), final_path))
        os.replace(temp_path, final_path)
        transaction.record_installed(new_filename)
    except Exception as e:
        logging.error(tr("Ошибка при замене файла для мода '{0}': {1}. Восстановление старой версии...", mod_name, e))
        for backup_path, original_path in reversed(backups):
            try:
                transaction.restore_file(backup_path, original_path)
            except Exception as restore_error:
                logging.critical(tr('Не удалось восстановить {0}: {1}', original_path, restore_error))
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        return ModStatus.REPLACE_ERROR

    status = ModStatus.UPDATED if mod.get('filepath') else ModStatus.INSTALLED
    logging.info(tr("Мод '{0}' успешно {1} до версии {2}.", mod_name, tr('обновлен') if status == ModStatus.UPDATED else tr('установлен'), mod.get('new_version')))
    return status


def remove_mods(filepaths: Iterable[str], transaction: UpdateTransaction) -> tuple[List[str], List[str]]:
    """Переносит файлы в бэкап транзакции. Возвращает (удалённые, ошибки)."""
    removed, errors = [], []
    for path in filepaths:
        if not path or not os.path.exists(path):
            continue
        try:
            transaction.backup_file(path)
            removed.append(path)
        except Exception as e:
            errors.append(f"{os.path.basename(path)}: {e}")
            logging.error(tr('Не удалось удалить {0}: {1}', path, e))
    return removed, errors


def move_all_to_backup(mods_path: str, transaction: UpdateTransaction) -> None:
    """Для режима «Заменить все»: переносит всё содержимое mods (кроме папки бэкапов) в транзакцию."""
    backup_dir = os.path.normcase(os.path.normpath(backup_root(mods_path)))
    for item_name in os.listdir(mods_path):
        source_path = os.path.join(mods_path, item_name)
        if os.path.normcase(os.path.normpath(source_path)) == backup_dir:
            continue
        transaction.backup_file(source_path)


def add_files(paths: Iterable[str], mods_path: str, transaction: UpdateTransaction) -> tuple[List[str], List[str]]:
    """Копирует jar-файлы в папку mods (перетаскивание). Файл с тем же именем уходит в бэкап."""
    added, errors = [], []
    os.makedirs(mods_path, exist_ok=True)
    for source in paths:
        name = os.path.basename(source)
        target = os.path.join(mods_path, name)
        try:
            if os.path.normcase(os.path.abspath(source)) == os.path.normcase(os.path.abspath(target)):
                continue
            if os.path.exists(target):
                transaction.backup_file(target)
            shutil.copy2(source, target)
            transaction.record_installed(name)
            added.append(name)
        except OSError as e:
            errors.append(f"{name}: {e}")
            logging.error(tr('Не удалось добавить {0}: {1}', source, e))
    return added, errors


# --- Снимки сборки ---
# Снимок — полная копия набора файлов папки (моды целиком). Файлы связываются жёсткими ссылками,
# если это возможно: копия почти не занимает места, пока исходный файл не удалён.

def snapshots_root(folder: str) -> str:
    return os.path.join(backup_root(folder), SNAPSHOTS_DIR_NAME)


def _link_or_copy(source: str, target: str):
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _content_files(folder: str, content: ContentType) -> List[str]:
    if not os.path.isdir(folder):
        return []
    return sorted(f for f in os.listdir(folder) if content.matches(f) and os.path.isfile(os.path.join(folder, f)))


def create_snapshot(folder: str, reason: str = "manual", content: ContentType = MODS, title: str = "") -> Optional[Dict]:
    """Сохраняет текущий набор файлов. Возвращает манифест или None, если сохранять нечего."""
    files = _content_files(folder, content)
    if not files:
        return None
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
    os.makedirs(snapshots_root(folder), exist_ok=True)
    snap_dir = make_unique_dir(os.path.join(snapshots_root(folder), timestamp))
    entries, total = [], 0
    for name in files:
        source = os.path.join(folder, name)
        _link_or_copy(source, os.path.join(snap_dir, name))
        size = os.path.getsize(source)
        total += size
        entries.append({"name": name, "size": size})
    manifest = {"created": datetime.datetime.now().isoformat(timespec="seconds"), "reason": reason,
                "title": title, "content": content.key, "files": entries, "total_size": total}
    with open(os.path.join(snap_dir, MANIFEST_NAME), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    manifest["dir"] = snap_dir
    logging.info(tr('Создан снимок сборки ({0} файлов): {1}', len(entries), snap_dir))
    return manifest


def list_snapshots(folder: str) -> List[Dict]:
    """Снимки папки, новые первыми."""
    root = snapshots_root(folder)
    if not os.path.isdir(root):
        return []
    result = []
    for name in sorted(os.listdir(root), reverse=True):
        try:
            with open(os.path.join(root, name, MANIFEST_NAME), "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        manifest["dir"] = os.path.join(root, name)
        result.append(manifest)
    return result


def delete_snapshot(snapshot: Dict):
    shutil.rmtree(snapshot["dir"], ignore_errors=True)


def prune_snapshots(folder: str, keep: int):
    """Удаляет старые автоматические снимки сверх лимита (созданные вручную не трогаются)."""
    auto = [s for s in list_snapshots(folder) if s.get("reason") != "manual"]
    for snapshot in auto[max(0, keep):]:
        delete_snapshot(snapshot)


def restore_snapshot(folder: str, snapshot: Dict, content: ContentType = MODS) -> tuple[UpdateTransaction, List[str]]:
    """
    Возвращает папку к состоянию снимка. Текущие файлы уходят в транзакцию «восстановление снимка»,
    поэтому само восстановление тоже можно отменить на странице «История».
    """
    transaction = UpdateTransaction(folder, "restore")
    errors = []
    for name in _content_files(folder, content):
        try:
            transaction.backup_file(os.path.join(folder, name))
        except OSError as e:
            errors.append(f"{name}: {e}")
    for item in snapshot.get("files", []):
        name = safe_filename(item["name"])
        source = os.path.join(snapshot["dir"], name)
        target = os.path.join(folder, name)
        try:
            if os.path.exists(target):
                continue
            _link_or_copy(source, target)
            transaction.record_installed(name)
        except OSError as e:
            errors.append(f"{name}: {e}")
    return transaction, errors
