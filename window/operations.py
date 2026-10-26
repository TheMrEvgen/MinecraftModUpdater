"""Фоновые операции: проверка обновлений, обновление и установка, удаление, откат.

Примесь к главному окну (ModUpdaterApp): методы работают с его атрибутами."""
import logging
import os
from typing import Callable, Dict, List, Optional

from PyQt6.QtWidgets import QMessageBox

import config as app_config
import downloads
from content import DISABLED_SUFFIX, MODS
from enums import ModStatus
from i18n import plural, tr
from mod_updater_logic import format_date, select_primary_file
from scanner import collect_changelogs
from ui.dialogs import UpdateReviewDialog
from ui.mod_list import entry_at
from ui_components import ask_choice, confirm, show_message
from updater import (
    UpdateTransaction, find_locked_files, is_game_running, list_transactions, move_all_to_backup, remove_mods,
    rollback_transaction,
)
from view_model import CAT_PROBLEMS, CAT_UPDATES, category_counts, entry_key, iter_all_entries
from workers import LocalScanWorker, ScanWorker, TaskWorker, UpdateWorker, fetch_versions


class OperationsMixin:
    # ================================================================== Фоновые операции

    def _set_busy(self, kind: Optional[str], text: str = "", determinate: bool = False):
        self._busy = kind
        self.progress_box.setVisible(kind is not None)
        self.status_label.setText(text)
        if not determinate:
            self.slim_progress.setRange(0, 0)
        self.sidebar.profile_list.setEnabled(kind is None)
        if self.taskbar:
            if kind is None:
                self.taskbar.clear()
            elif not determinate:
                self.taskbar.busy()
        self._refresh_actions()
        self._refresh_selection_ui()

    def start_local_scan(self):
        if self._busy or not self.mods_path:
            return
        if not os.path.isdir(self.mods_path):
            self._set_data([])
            return
        self._set_busy("local", tr('Читаем папку {0}…', self.content.folder))
        self._refresh_empty_state()
        self.local_scan_worker = LocalScanWorker(self.mods_path, app_config.CACHE_FILE, self.content)
        self.local_scan_worker.finished.connect(self._on_local_scan_finished)
        self.local_scan_worker.start()

    def _on_local_scan_finished(self, mods_data):
        self.local_scan_worker = None
        self._set_busy(None)
        if mods_data is None:
            return
        self._set_data(mods_data)
        self.sessions[self._skey()] = mods_data
        if mods_data and self.settings.get("auto_check", True):
            self.start_scan(silent=True)

    def _on_check_clicked(self):
        if self._busy in ("scan", "local", "import"):
            worker = self.scan_worker or self.local_scan_worker or self.import_worker
            if worker:
                worker.stop()
                self.status_label.setText(tr("Отмена…"))
            return
        self.start_scan()

    def start_scan(self, silent: bool = False):
        if self._busy or not self.mods_path or not os.path.isdir(self.mods_path) or self.is_import_mode:
            return
        if not self.game_version or not self.loader:
            self.toasts.show(tr("Не удалось определить версию игры или загрузчик — укажите их в настройках сборки."),
                             "warning", tr("Настроить"), self._edit_current_profile)
            return
        self._silent_scan = silent
        channel = self._channel()
        logging.info(tr('Проверка обновлений: Minecraft {0} ({1}), канал {2}', self.game_version, self.loader, channel.value))
        self._set_busy("scan", tr("Проверяем обновления…"))
        self.scan_worker = ScanWorker(self.mods_path, self.game_version, self.loader, app_config.CACHE_FILE, channel,
                                      self.content)
        self.scan_worker.finished.connect(self._on_scan_finished)
        self.scan_worker.progress.connect(self.status_label.setText)
        self.scan_worker.start()

    def _on_scan_finished(self, mods_data):
        self.scan_worker = None
        self._set_busy(None)
        if mods_data is None:
            self.toasts.show(tr("Проверка отменена."), "info")
            return
        if not mods_data and self.mods_data:
            self.toasts.show(tr("Не удалось проверить обновления. Проверьте подключение к интернету."), "error")
            return
        fresh = self._mark_new_updates(mods_data)
        self._set_data(mods_data)
        self.sessions[self._skey()] = mods_data
        counts = category_counts(self._count_items)
        if fresh:
            names = ", ".join(e.get("name") for e in fresh[:3]) + (tr(' и ещё {0}', len(fresh) - 3) if len(fresh) > 3 else "")
            self.toasts.show(tr('С прошлого запуска вышли новые версии: {0}', names), "info", tr("Показать"),
                             lambda: (self._reset_filters(), self.search.setText("is:new")), timeout=10000)
        updates, problems = counts[CAT_UPDATES], counts[CAT_PROBLEMS]
        if updates:
            self.toasts.show(tr('Доступно обновлений: {0}', updates), "info", tr("Обновить все"), self.update_all, timeout=8000)
        elif not self._silent_scan:
            self.toasts.show(tr("Все моды актуальны."), "success")
        if problems:
            self.toasts.show(tr('Найдены проблемы с зависимостями: {0}', problems), "warning", tr("Показать"),
                             lambda: self._set_category(CAT_PROBLEMS), timeout=8000)
        self._maybe_check_readiness()

    def _run_task(self, func, args, on_success: Callable, on_failure: Callable, busy_text: Optional[str]):
        """Разовая блокирующая операция в фоне. busy_text=None — не блокировать интерфейс (поиск в каталоге)."""
        worker = TaskWorker(func, *args)
        self._task_workers.append(worker)
        previous_busy = self._busy if busy_text is not None else "background"
        if previous_busy is None:
            self._set_busy("task", busy_text)

        def done():
            if worker in self._task_workers:
                self._task_workers.remove(worker)
            if previous_busy is None and self._busy == "task":
                self._set_busy(None)

        worker.succeeded.connect(lambda result: (done(), on_success(result)))
        worker.failed.connect(lambda error: (done(), on_failure(error)))
        worker.start()

    # ================================================================== Обновление и установка

    def update_all(self):
        entries = self._bulk_candidates()
        if not entries or self._busy:
            return
        if self.settings.get("changelog_before_update", True) and not self.is_import_mode:
            game_version, loader, content = self.game_version, self.loader, self.content
            dialog = UpdateReviewDialog(
                self, self.tokens, entries, self._icon_for_entry,
                lambda callback: self._run_task(collect_changelogs, (entries, game_version, loader, content),
                                                callback, lambda e: callback({}), None))
            if not dialog.exec():
                return
            entries = dialog.selected_entries()
            if not entries:
                return
        if not self.is_import_mode:
            self._auto_snapshot("update_all")
        self.start_update(entries)

    def _update_checked(self):
        self.start_update([e for e in self._checked_entries() if e.get("download_url")])

    def _confirm_game_not_running(self, paths: List[str]) -> bool:
        if self._game_running:
            self.toasts.show(tr("Игра запущена — закройте её, чтобы менять моды."), "warning", tr("Закрыть игру"), self._stop_game)
            return False
        return self._ask_game_not_running(paths)

    def _ask_game_not_running(self, paths: List[str]) -> bool:
        while True:
            locked = find_locked_files([p for p in paths if p])
            if not locked and not is_game_running(self.mods_path):
                return True
            names = "\n".join(f"• {os.path.basename(p)}" for p in locked[:5])
            text = (tr("Похоже, Minecraft запущен: файлы модов заблокированы.\n"
                    "Закройте игру, иначе файлы не удастся заменить."))
            if names:
                text += tr('\n\nЗаблокированы:\n{0}', names)
            choice = ask_choice(self, QMessageBox.Icon.Warning, tr("Игра запущена?"), text,
                                [tr("Проверить снова"), tr("Продолжить всё равно"), tr("Отмена")], default=0)
            if choice == 1:
                return True
            if choice != 0:
                return False

    def start_update(self, entries: List[dict], transaction: Optional[UpdateTransaction] = None,
                     folder: Optional[str] = None, job: Optional[downloads.DownloadJob] = None):
        """Скачивает и ставит entries в folder (по умолчанию — папка текущей вкладки). job — повтор прежней загрузки."""
        if self._busy:
            self.toasts.show(tr("Дождитесь окончания текущей операции."), "warning")
            return
        folder = folder or self.mods_path
        unique: Dict[object, dict] = {}
        for entry in entries:
            if entry.get("download_url") and entry_key(entry) not in unique:
                unique[entry_key(entry)] = entry
        entries = list(unique.values())
        if not entries:
            return
        if not folder:
            show_message(self, QMessageBox.Icon.Warning, tr("Нет папки игры"), tr("Сначала укажите папку игры в настройках сборки."))
            return
        os.makedirs(folder, exist_ok=True)
        if self.is_import_mode and transaction is None:
            proceed, transaction = self._ask_import_install_method()
            if not proceed:
                return
        if transaction is None and not self._confirm_game_not_running([e.get("filepath") for e in entries]):
            return

        self._update_done = 0
        self.slim_progress.setRange(0, len(entries))
        self.slim_progress.setValue(0)
        self._set_busy("update", tr('Обновление: 0 из {0}', len(entries)), determinate=True)
        if self.taskbar:
            self.taskbar.set_value(0, len(entries))
        self._update_folder = folder
        self.update_worker = UpdateWorker(entries, folder, transaction,
                                          max_workers=self.settings.get("parallel_downloads", 3))
        worker = self.update_worker
        self._update_keys = [str(entry_key(e)) for e in entries]
        if job:  # «Повторить»: та же строка в «Загрузках», снова качаются только неудавшиеся
            job.cancel_callback = worker.stop
            job.restart(self._update_keys)
            self._start_download_timer()
        else:
            job = self._start_download(
                tr("Моды: {0}", self.current_profile_name) if len(entries) > 1 else entries[0].get("name") or "?",
                "mods", worker.stop)
            for key, entry in zip(self._update_keys, entries):
                job.add_file(key, entry.get("name") or entry.get("new_filename") or "?", entry.get("new_size") or 0)
        self._update_job = worker.download_job = job
        self.update_worker.progress.connect(self._on_update_progress)
        self.update_worker.download_progress.connect(self._on_download_progress)
        self.update_worker.finished.connect(self._on_update_finished)
        self.update_worker.start()

    def _entries_like(self, entry: dict) -> List[dict]:
        key = entry_key(entry)
        return [e for e in self._all_entries() if entry_key(e) == key]

    def _on_update_progress(self, index: int, status_value: str):
        if not self.update_worker:
            return
        source = self.update_worker.mods_to_update[index]
        status = ModStatus(status_value)
        success = status in (ModStatus.UPDATED, ModStatus.INSTALLED)
        job = getattr(self, "_update_job", None)
        if job:
            key = self._update_keys[index]
            if status == ModStatus.DOWNLOADING:
                job.file_started(key)
            elif status != ModStatus.FINISHING:
                job.file_done(key, status.value if status.is_error else "", cancelled=status == ModStatus.CANCELLED)
        for entry in self._entries_like(source):
            entry["status"] = status
            entry.pop("_progress", None)
            if success:
                new_filename = source.get("new_filename")
                if new_filename and entry.get("disabled"):
                    new_filename += DISABLED_SUFFIX
                entry.update({
                    "current_version": source.get("new_version", ""), "new_version": "", "new_version_date": "",
                    "download_url": None, "new_filename": None, "new_hashes": None, "installed": True,
                    "filepath": os.path.join(self._update_folder, new_filename) if new_filename else entry.get("filepath"),
                    "size": source.get("new_size") or entry.get("size"), "new_size": 0,
                    "installed_info": source.get("_latest_info") or entry.get("installed_info"),
                    "_is_checked": False,
                })
        if status != ModStatus.DOWNLOADING:
            self._update_done += 1
            total = len(self.update_worker.mods_to_update)
            self.slim_progress.setValue(self._update_done)
            self.status_label.setText(tr('Обновление: {0} из {1}', self._update_done, total))
            if self.taskbar:
                self.taskbar.set_value(self._update_done, total, error=status.is_error)
        self._refresh_project(source.get("project_id"))
        if self.details.entry and entry_key(self.details.entry) == entry_key(source):
            self.details.set_entry(self.details.entry, self._icon_for_entry(self.details.entry))

    def _on_download_progress(self, index: int, downloaded: int, total: int):
        if not self.update_worker or total <= 0:
            return
        job = getattr(self, "_update_job", None)
        if job:
            job.file_progress(self._update_keys[index], downloaded, total)
        source = self.update_worker.mods_to_update[index]
        for entry in self._entries_like(source):
            entry["_progress"] = downloaded / total
        self._refresh_project(source.get("project_id"))

    def _on_update_finished(self):
        worker = self.update_worker
        self.update_worker = None
        self._set_busy(None)
        if not worker:
            return
        results = worker.results
        failed = [worker.mods_to_update[i] for i, s in results.items() if s.is_error]
        succeeded = sum(1 for s in results.values() if s in (ModStatus.UPDATED, ModStatus.INSTALLED))
        job, self._update_job = getattr(self, "_update_job", None), None
        if job:
            cancelled = not worker.is_running
            job.finish(downloads.CANCELLED if cancelled else downloads.FAILED if failed else downloads.DONE,
                       tr("Установлено: {0}", succeeded) + (tr(", ошибок: {0}", len(failed)) if failed else ""))
            retry = [worker.mods_to_update[i] for i, s in results.items() if s.is_error or s == ModStatus.CANCELLED]
            folder = worker.mods_path
            job.retry_callback = (lambda: self.start_update(retry, folder=folder, job=job)) if retry else None
            if failed:
                self._notify_download(tr("Не всё скачалось"), tr("Не удалось обновить: {0}", len(failed)))
            elif succeeded and not cancelled:
                self._notify_download(tr("Моды обновлены"), job.summary)
        transaction = worker.transaction
        if self._catalog_installing:
            self._finish_catalog_install(worker)
            return

        if self.is_import_mode and succeeded:
            self.is_import_mode = False
            self.banner.hide()
            self.sessions.pop(self._skey(), None)
            self.start_local_scan()
        else:
            self._set_data(self.mods_data)  # Пересчёт проблем и счётчиков
            self.sessions[self._skey()] = self.mods_data

        if succeeded:
            self._push_undo(tr("обновление"), lambda: self._rollback_dir(transaction.dir))
            self._prune_backups()
            self.toasts.show(tr('Готово: {0} {1} обновлено', succeeded, plural(succeeded, 'мод', 'мода', 'модов')),
                             "success", tr("Отменить"), lambda: self._rollback_dir(transaction.dir), timeout=9000)
        if failed:
            names = "\n".join(f"• {e.get('name')}: {e.get('status')}" for e in failed[:15])
            self.toasts.show(tr('Не удалось обновить: {0}', len(failed)), "error", tr("Подробнее"),
                             lambda: show_message(self, QMessageBox.Icon.Warning, tr("Ошибки обновления"),
                                                  tr('{0}\n\nПодробности на странице «Журнал».', names)), timeout=12000)

    def _ask_import_install_method(self):
        choice = ask_choice(
            self, QMessageBox.Icon.Question, tr("Как установить сборку?"),
            tr("<b>Заменить все:</b> текущие моды уйдут в бэкап, будет установлена только новая сборка.<br><br>"
            "<b>Объединить:</b> моды из сборки добавятся к текущим, остальные останутся.<br><br>"
            "<span style='opacity:0.7'>Любой вариант можно отменить на странице «История».</span>"),
            [tr("Заменить все"), tr("Объединить"), tr("Отмена")], default=0, rich_text=True)
        if choice not in (0, 1):
            return False, None
        jars = [os.path.join(self.mods_path, f) for f in os.listdir(self.mods_path) if f.lower().endswith(".jar")]
        if not self._confirm_game_not_running(jars):
            return False, None
        transaction = UpdateTransaction(self.mods_path, "replace_all" if choice == 0 else "update")
        if choice == 1:
            # Объединение: если мод из сборки уже установлен, новая версия заменяет старый файл, а не ложится рядом
            installed = {e["project_id"]: e["filepath"]
                         for e in iter_all_entries(self.sessions.get(self._skey(content=MODS)) or [])
                         if e.get("project_id") and e.get("filepath") and os.path.exists(e["filepath"])}
            for entry in self.model.items():
                if not entry.get("filepath") and entry.get("project_id") in installed:
                    entry["filepath"] = installed[entry["project_id"]]
        if choice == 0:
            self._auto_snapshot("replace_all")
            try:
                move_all_to_backup(self.mods_path, transaction)
            except Exception as e:
                logging.error(tr('Ошибка при переносе модов в бэкап: {0}', e))
                show_message(self, QMessageBox.Icon.Critical, tr("Ошибка бэкапа"),
                             tr('Не удалось перенести моды в бэкап: {0}\nУже перенесённые файлы можно вернуть в «Истории».', e))
                return False, None
        return True, transaction

    def _load_versions(self, entry: dict):
        if not entry.get("project_id"):
            return
        self._run_task(fetch_versions, (entry["project_id"], self.game_version, self.loader),
                       lambda versions: self.details.entry is entry and self.details.set_versions(versions),
                       lambda e: self.toasts.show(tr('Не удалось загрузить версии: {0}', e), "error"),
                       tr("Загружаем список версий…"))

    def _install_specific_version(self, entry: dict, version: dict):
        primary = select_primary_file(version)
        if not primary:
            return
        entry.update({
            "new_version": version.get("version_number", ""), "new_version_date": format_date(version.get("date_published")),
            "download_url": primary["url"], "new_filename": primary["filename"], "new_hashes": primary.get("hashes"),
            "_latest_info": version,
        })
        if not entry.get("filepath"):
            entry["status"] = ModStatus.READY_TO_INSTALL
        self.start_update([entry])

    # ================================================================== Удаление и откат

    def _delete_checked(self):
        self._delete_entries([e for e in self._checked_entries() if e.get("filepath")])

    def _delete_checked_or_current(self):
        checked = [e for e in self._checked_entries() if e.get("filepath")]
        if checked:
            self._delete_entries(checked)
        else:
            entry = entry_at(self.view.currentIndex())
            if entry and entry.get("filepath"):
                self._delete_entries([entry])

    def _delete_entries(self, entries: List[dict]):
        paths = list(dict.fromkeys(e["filepath"] for e in entries if e.get("filepath")))
        if not paths or self._busy:
            return
        what = f"«{entries[0].get('name')}»" if len(paths) == 1 else tr('{0} модов', len(paths))
        if not confirm(self, tr("Удаление"), tr('Удалить {0}?\nФайлы уйдут в бэкап — удаление можно отменить.', what),
                       tr("Удалить"), tr("Отмена")):
            return
        if not self._confirm_game_not_running(paths):
            return
        transaction = UpdateTransaction(self.mods_path, "delete")
        removed, errors = remove_mods(paths, transaction)
        removed_set = {os.path.normcase(os.path.normpath(p)) for p in removed}

        def keep(entry):
            fp = entry.get("filepath")
            return not (fp and os.path.normcase(os.path.normpath(fp)) in removed_set)

        mods_data = [m for m in self.mods_data if keep(m)]
        for mod in mods_data:
            if mod.get("dependencies"):
                mod["dependencies"] = [d for d in mod["dependencies"] if keep(d)]
        self._set_data(mods_data)
        self.sessions[self._skey()] = mods_data
        if removed:
            self._push_undo(tr("удаление"), lambda: self._rollback_dir(transaction.dir))
            self.toasts.show(tr('Удалено: {0}', len(removed)), "success", tr("Отменить"),
                             lambda: self._rollback_dir(transaction.dir), timeout=9000)
        if errors:
            show_message(self, QMessageBox.Icon.Critical, tr("Ошибка удаления"), "\n".join(errors[:10]))

    def _rollback_dir(self, tx_dir: str):
        # <папка>/ModUpdater_Backups/transactions/<время>: операция могла быть в другой папке (установка из каталога)
        folder = os.path.dirname(os.path.dirname(os.path.dirname(tx_dir)))
        manifest = next((m for m in list_transactions(folder) if os.path.normcase(m["dir"]) == os.path.normcase(tx_dir)), None)
        if not manifest:
            self.toasts.show(tr("Эта операция уже отменена."), "info")
            return
        self._do_rollback(manifest, folder)

    def _rollback_from_history(self, manifest: dict, folder: Optional[str] = None):
        folder = folder or self.mods_path
        transactions = list_transactions(folder)
        is_latest = bool(transactions) and transactions[0]["dir"] == manifest["dir"]
        warning = "" if is_latest else (tr("\n\n⚠ Это не последняя операция: более поздние изменения тех же файлов "
                                         "могут конфликтовать — такие файлы будут пропущены."))
        if confirm(self, tr("Вернуть изменения"), tr("Удалить файлы, установленные этой операцией, и вернуть прежние?")
                   + warning, tr("Вернуть"), tr("Отмена")):
            self._do_rollback(manifest, folder)

    def _do_rollback(self, manifest: dict, folder: Optional[str] = None):
        if self._busy:
            self.toasts.show(tr("Дождитесь окончания текущей операции."), "warning")
            return
        folder = folder or self.mods_path
        paths = [os.path.join(folder, f) for f in manifest.get("installed", [])]
        if not self._confirm_game_not_running(paths):
            return
        errors = rollback_transaction(folder, manifest)
        if errors:
            show_message(self, QMessageBox.Icon.Warning, tr("Откат выполнен частично"), "\n".join(errors[:10]))
        else:
            self.toasts.show(tr("Изменения отменены."), "success")
        self.is_import_mode = False
        self.banner.hide()
        self._invalidate_folder(folder)
        self._refresh_history()
        if os.path.normcase(folder) == os.path.normcase(self.mods_path):
            self.start_local_scan()
