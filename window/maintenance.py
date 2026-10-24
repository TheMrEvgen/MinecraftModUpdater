"""Обслуживание: целостность файлов, бэкапы, перенос настроек.

Примесь к главному окну (ModUpdaterApp): методы работают с его атрибутами."""
import logging
import os
from typing import Dict, List

from PyQt6.QtWidgets import QFileDialog, QMessageBox

import config as app_config
from i18n import tr
from maintenance import backups_usage, check_integrity, prune_backups
from ui.tools_dialogs import IntegrityDialog
from ui_components import ask_choice, show_message
from updater import UpdateTransaction


class MaintenanceMixin:
    # ================================================================== Целостность и бэкапы

    def _check_integrity(self):
        if not self.mods_path or self._busy:
            return
        result = check_integrity(self.mods_path, self.content)
        dialog = IntegrityDialog(self, self.tokens, result)
        dialog.fix_requested.connect(lambda: self._fix_integrity(result))
        dialog.exec()

    def _fix_integrity(self, result: Dict[str, List[str]]):
        paths = result["broken"] + result["empty"] + result["partial"]
        if not self._confirm_game_not_running(paths):
            return
        transaction = UpdateTransaction(self.mods_path, "delete")
        errors = []
        for path in paths:
            try:
                transaction.backup_file(path)
            except OSError as e:
                errors.append(f"{os.path.basename(path)}: {e}")
        self.toasts.show(tr('Убрано в бэкап: {0}', len(paths) - len(errors)), "success", tr("Отменить"),
                         lambda: self._rollback_dir(transaction.dir), timeout=9000)
        if errors:
            show_message(self, QMessageBox.Icon.Warning, tr("Не всё удалось"), "\n".join(errors[:10]))
        self.sessions.pop(self._skey(), None)
        self.start_local_scan()

    def _backup_usage_text(self) -> str:
        if not self.mods_path or not os.path.isdir(self.mods_path):
            return ""
        from view_model import format_size
        usage = backups_usage(self.mods_path)
        total = sum(usage.values())
        return (tr('Бэкапы папки {0} занимают {1} (операции {2}, снимки {3}).', self.content.folder, format_size(total), format_size(usage['transactions']), format_size(usage['snapshots'])))

    def _prune_backups(self, manual: bool = False):
        if not self.mods_path or not os.path.isdir(self.mods_path):
            return
        days = int(self.settings.get("backup_max_days", 30)) or None
        megabytes = int(self.settings.get("backup_max_mb", 1024))
        try:
            removed = prune_backups(self.mods_path, days, megabytes * 1024 * 1024 if megabytes else None)
        except OSError as e:
            logging.error(tr('Очистка бэкапов: {0}', e))
            removed = 0
        if manual:
            self.toasts.show(tr('Удалено старых бэкапов: {0}.', removed) if removed else tr("Нечего удалять — всё в пределах лимитов."),
                             "success" if removed else "info")
            self.settings_page.set_backup_usage(self._backup_usage_text())

    # ================================================================== Перенос настроек

    def _config_snapshot(self) -> dict:
        return {"theme": self.config_theme, "current_profile": self.current_profile_name, "profiles": self.profiles,
                "settings": self.settings, "hidden_instances": self.hidden_instances, "window": self.window_cfg,
                "mod_tags": self.mod_tags, "mod_notes": self.mod_notes, "favorites": self.favorites,
                "last_seen": self.last_seen}

    def _export_settings(self):
        path, _ = QFileDialog.getSaveFileName(self, tr("Экспорт настроек"), "mod-updater-settings.json", "JSON (*.json)")
        if not path:
            return
        try:
            app_config.export_config(self._config_snapshot(), path)
            self.toasts.show(tr('Настройки сохранены в {0}', os.path.basename(path)), "success")
        except OSError as e:
            show_message(self, QMessageBox.Icon.Critical, tr("Ошибка"), tr('Не удалось сохранить файл:\n{0}', e))

    def _import_settings(self):
        if self._busy:
            return
        path, _ = QFileDialog.getOpenFileName(self, tr("Импорт настроек"), "", "JSON (*.json)")
        if not path:
            return
        try:
            imported = app_config.import_config(path)
        except ValueError as e:
            show_message(self, QMessageBox.Icon.Warning, tr("Импорт настроек"), str(e))
            return
        choice = ask_choice(self, QMessageBox.Icon.Question, tr("Импорт настроек"),
                            tr('В файле {0} сборок, {1} заметок, {2} избранных.\n\nЗаменить текущие настройки или добавить сборки и заметки к имеющимся?', len(imported['profiles']), len(imported['mod_notes']), len(imported['favorites'])), [tr("Добавить"), tr("Заменить всё"), tr("Отмена")], default=0)
        if choice not in (0, 1):
            return
        if choice == 1:
            self.profiles = imported["profiles"]
            self.settings.clear()
            self.settings.update(imported["settings"])
            self.mod_tags.clear()
            self.mod_tags.update(imported["mod_tags"])
            self.mod_notes.clear()
            self.mod_notes.update(imported["mod_notes"])
            self.favorites[:] = imported["favorites"]
            self.config_theme = imported.get("theme", self.config_theme)
        else:
            for name, profile in imported["profiles"].items():
                self.profiles.setdefault(name if name not in self.profiles else tr('{0} (импорт)', name), profile)
            for key, tags in imported["mod_tags"].items():
                self.mod_tags.setdefault(key, tags)
            for key, note in imported["mod_notes"].items():
                self.mod_notes.setdefault(key, note)
            self.favorites.extend(k for k in imported["favorites"] if k not in self.favorites)
        self.sessions.clear()
        self.save_config()
        self.settings_page.load(self.config_theme, self.settings)
        self.apply_theme()
        self._setup_action_shortcuts()
        self._refresh_sidebar()
        self._load_profile(self.current_profile_name if self.current_profile_name in self.profiles
                           else next(iter(self.profiles)))
        self.toasts.show(tr("Настройки импортированы."), "success")

    # ================================================================== Все сборки: поиск и место на диске

    def _show_all_packs(self):
        from ui.all_packs_dialog import AllPacksDialog
        dialog = AllPacksDialog(self, self.tokens, self.profiles, app_config.CACHE_FILE, self._run_task,
                                on_changed=lambda: self.sessions.clear())
        dialog.exec()
