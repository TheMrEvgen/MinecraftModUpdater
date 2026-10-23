"""Импорт и экспорт: список модов, синхронизация, .mrpack, JSON для сервера, перетаскивание.

Примесь к главному окну (ModUpdaterApp): методы работают с его атрибутами."""
import os
from typing import List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox

from content import MODS
from exporters import (
    build_mrpack_index, build_share_data, decode_share_string, encode_share_string, parse_share_data,
    save_server_json, write_mrpack,
)
from i18n import tr
from mod_updater_logic import get_latest_loader_version
from scanner import entries_from_mrpack, mrpack_game_info, read_mrpack
from ui_components import ImportDialog, ShareDialog, SyncDialog, ask_choice, confirm, show_message
from updater import UpdateTransaction, add_files
from view_model import CAT_ALL
from window.common import PAGE_MODS
from workers import ImportWorker, fetch_json


class TransferMixin:
    # ================================================================== Импорт

    def import_mod_list(self):
        dialog = ImportDialog(self)
        if dialog.exec():
            self._import_text(dialog.get_share_string())

    def _import_text(self, text: str):
        text = (text or "").strip()
        if not text:
            return
        if text.lower().startswith(("http://", "https://")):
            self._sync_url(text)
            return
        try:
            self._process_imported_data(decode_share_string(text))
        except ValueError as e:
            show_message(self, QMessageBox.Icon.Critical, tr("Ошибка импорта"), tr('Не удалось прочитать строку: {0}', e))

    def sync_from_url(self):
        dialog = SyncDialog(self)
        if dialog.exec() and dialog.get_url():
            self._sync_url(dialog.get_url())

    def _sync_url(self, url: str):
        if not url.lower().startswith(("http://", "https://")):
            show_message(self, QMessageBox.Icon.Warning, tr("Ошибка"), tr("Укажите адрес, начинающийся с http:// или https://"))
            return
        self._run_task(fetch_json, (url,), self._process_imported_data,
                       lambda e: show_message(self, QMessageBox.Icon.Critical, tr("Ошибка"), tr('Не удалось загрузить данные:\n{0}', e)),
                       tr("Загружаем сборку…"))

    def _process_imported_data(self, share_data):
        try:
            request = parse_share_data(share_data)
        except ValueError as e:
            show_message(self, QMessageBox.Icon.Critical, tr("Ошибка"), tr('Не удалось прочитать данные:\n{0}', e))
            return
        use_exact = False
        if request.version >= 2:
            strict_text = (tr("Автор закрепил точные версии модов.") if request.strict
                           else tr("Автор не закреплял версии."))
            choice = ask_choice(self, QMessageBox.Icon.Question, tr("Импорт сборки"),
                                tr('Minecraft {0} ({1}), модов: {2}.\n{3}\n\nКакие версии установить?', request.game_version, request.loader, len(request.project_ids), strict_text),
                                [tr("Точные версии"), tr("Самые новые"), tr("Отмена")], default=0 if request.strict else 1)
            if choice not in (0, 1):
                return
            use_exact = choice == 0
        elif not confirm(self, tr("Импорт сборки"),
                         tr('Импортировать {0} модов для Minecraft {1} ({2})?', len(request.project_ids), request.game_version, request.loader)):
            return
        if self._busy:
            return
        self._set_busy("import", tr("Ищем совместимые версии…"))
        self._import_meta = (request.game_version, request.loader)
        self.import_worker = ImportWorker(request.target_mods, request.game_version, request.loader, use_exact,
                                          self._channel())
        self.import_worker.finished.connect(self._on_import_finished)
        self.import_worker.progress.connect(self.status_label.setText)
        self.import_worker.start()

    def _on_import_finished(self, entries):
        self.import_worker = None
        self._set_busy(None)
        if entries is None:
            self.toasts.show(tr("Импорт отменён."), "info")
            return
        if not entries:
            self.toasts.show(tr("Не удалось найти совместимые версии модов."), "error")
            return
        game_version, loader = self._import_meta
        self._enter_import_mode(entries, game_version, loader)

    def import_mrpack_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("Открыть сборку"), "", "Modrinth Modpack (*.mrpack)")
        if path:
            self._import_mrpack(path)

    def _import_mrpack(self, path: str):
        try:
            index = read_mrpack(path)
        except ValueError as e:
            show_message(self, QMessageBox.Icon.Critical, tr("Ошибка"), str(e))
            return
        game_version, loader = mrpack_game_info(index)
        name = index.get("name") or os.path.basename(path)
        self._run_task(entries_from_mrpack, (index,),
                       lambda entries: self._enter_import_mode(entries, game_version, loader, name),
                       lambda e: show_message(self, QMessageBox.Icon.Critical, tr("Ошибка"), tr('Не удалось прочитать сборку:\n{0}', e)),
                       tr("Читаем сборку…"))

    def _enter_import_mode(self, entries: List[dict], game_version: Optional[str], loader: Optional[str],
                           name: Optional[str] = None):
        if not entries:
            self.toasts.show(tr("В сборке не найдено модов для установки."), "warning")
            return
        if self.mods_data and not self.is_import_mode:
            self.sessions[self._skey()] = self.mods_data
        self.is_import_mode = True
        title = f"«{name}»" if name else tr("из списка")
        text = tr('<b>Импорт сборки {0}</b> — {1} модов, Minecraft {2} ({3}).', title, len(entries), game_version or '?', loader or '?')
        if (game_version and game_version != self.game_version) or (loader and loader != self.loader):
            text += (tr("<br><span style='color:{0}'>⚠ У этой сборки другая версия: у вас Minecraft {1} ({2}).</span>", self.tokens['warning'], self.game_version, self.loader))
        self.banner_text.setText(text)
        self.banner.show()
        self._close_details()
        self._set_data(entries, keep_selection=False)
        self._set_category(CAT_ALL)

    def _exit_import_mode(self):
        self.is_import_mode = False
        self.banner.hide()
        cached = self.sessions.get(self._skey(content=MODS))
        if cached is not None:
            self._set_data(cached)
        else:
            self._set_data([])
            self.start_local_scan()

    # ================================================================== Экспорт

    def _ask_strict(self, recipient: str) -> Optional[bool]:
        choice = ask_choice(self, QMessageBox.Icon.Question, tr("Настройки экспорта"),
                            tr('Закрепить текущие версии модов?\n\nДа — {0} точно такие же версии.\nНет — {1} самые новые версии.', recipient, recipient), [tr("Точные версии"), tr("Всегда новые"), tr("Отмена")], default=1)
        return None if choice not in (0, 1) else choice == 0

    def share_mod_list(self):
        share_data = build_share_data(self.mods_data, self.game_version, self.loader, strict=False)
        if not share_data["mods"]:
            self.toasts.show(tr("Нет модов с Modrinth — сначала проверьте обновления."), "warning")
            return
        strict = self._ask_strict(tr("получатель установит"))
        if strict is None:
            return
        share_data["strict"] = strict
        ShareDialog(encode_share_string(share_data), self).exec()

    def export_server_json(self):
        share_data = build_share_data(self.mods_data, self.game_version, self.loader, strict=False)
        if not share_data["mods"]:
            self.toasts.show(tr("Нет модов с Modrinth — сначала проверьте обновления."), "warning")
            return
        strict = self._ask_strict(tr("клиенты скачают"))
        if strict is None:
            return
        share_data["strict"] = strict
        path, _ = QFileDialog.getSaveFileName(self, tr("Сохранить JSON для сервера"), "modpack.json", "JSON (*.json)")
        if path:
            try:
                save_server_json(path, share_data)
                self.toasts.show(tr('Файл сохранён: {0}', os.path.basename(path)), "success")
            except OSError as e:
                show_message(self, QMessageBox.Icon.Critical, tr("Ошибка"), tr('Не удалось сохранить файл:\n{0}', e))

    def export_mrpack(self):
        loader_version = self.current_loader_version or self.latest_loader_version
        if not loader_version:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                loader_version = get_latest_loader_version(self.loader, self.game_version)
            finally:
                QApplication.restoreOverrideCursor()
        if not loader_version:
            show_message(self, QMessageBox.Icon.Warning, tr("Ошибка"),
                         tr('Не удалось определить версию загрузчика {0} для {1}.', self.loader, self.game_version))
            return
        index = build_mrpack_index(self.mods_data, self.game_version, self.loader, loader_version,
                                   name=self.current_profile_name)
        if not index["files"]:
            self.toasts.show(tr("Нет модов с Modrinth — сначала проверьте обновления."), "warning")
            return
        path, _ = QFileDialog.getSaveFileName(self, tr("Сохранить .mrpack"), f"{self.current_profile_name}.mrpack",
                                              "Modrinth Modpack (*.mrpack)")
        if not path:
            return
        try:
            write_mrpack(path, index)
            self.toasts.show(tr('Сборка ({0} модов) сохранена в {1}', len(index['files']), os.path.basename(path)), "success")
        except OSError as e:
            show_message(self, QMessageBox.Icon.Critical, tr("Ошибка"), tr('Не удалось сохранить файл:\n{0}', e))

    # ================================================================== Перетаскивание

    @staticmethod
    def _dropped_files(mime) -> List[str]:
        return [u.toLocalFile() for u in mime.urls() if u.isLocalFile()] if mime.hasUrls() else []

    def dragEnterEvent(self, event):
        files = self._dropped_files(event.mimeData())
        ok = any(f.lower().endswith((".jar", ".mrpack", ".zip")) for f in files) or (not files and event.mimeData().hasText())
        if ok and not self._busy:
            event.acceptProposedAction()
            self.drop_overlay.setGeometry(self.centralWidget().rect().adjusted(262, 12, -12, -12))
            self.drop_overlay.show()
            self.drop_overlay.raise_()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.drop_overlay.hide()

    def dropEvent(self, event):
        self.drop_overlay.hide()
        files = self._dropped_files(event.mimeData())
        event.acceptProposedAction()
        self.sidebar.set_page(PAGE_MODS)
        self.stack.setCurrentIndex(PAGE_MODS)
        packs = [f for f in files if f.lower().endswith(".mrpack")]
        jars = [f for f in files if f.lower().endswith(".jar")]
        zips = [f for f in files if f.lower().endswith(".zip")]
        if packs:
            self._import_mrpack(packs[0])
        elif jars:
            if self.content is not MODS:
                self._set_content(MODS)
            self._add_jars(jars)
        elif zips:
            if self.content is MODS:
                self.toasts.show(tr("Архивы .zip — это шейдеры или ресурспаки: откройте нужную вкладку и перетащите снова."),
                                 "info", timeout=7000)
                return
            self._add_jars(zips)
        elif event.mimeData().hasText():
            self._import_text(event.mimeData().text())

    def _add_jars(self, paths: List[str]):
        if not self.mods_path:
            self.toasts.show(tr("Сначала укажите папку игры для этой сборки."), "warning")
            return
        transaction = UpdateTransaction(self.mods_path, "add")
        added, errors = add_files(paths, self.mods_path, transaction)
        if added:
            self.toasts.show(tr('Добавлено файлов: {0}', len(added)), "success", tr("Отменить"),
                             lambda: self._rollback_dir(transaction.dir), timeout=9000)
            self.sessions.pop(self._skey(), None)
            self.start_local_scan()
        if errors:
            show_message(self, QMessageBox.Icon.Warning, tr("Не все файлы добавлены"), "\n".join(errors[:10]))
