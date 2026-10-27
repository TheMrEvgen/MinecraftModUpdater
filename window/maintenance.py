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
from content import MODS
from ui_components import ask_choice, show_message
from updater import UpdateTransaction

MALWARE_CACHE = "malware_scan.json"


class MaintenanceMixin:
    # ================================================================== Проверка модов на вирусы

    def _scan_malware(self, manual: bool = False):
        """Фоновая проверка jar-файлов сборки (после чтения папки — раз за сеанс) или по кнопке — заново все."""
        import malware_scan
        game_dir = self._profile().get("mods_path", "")
        if not game_dir:
            return
        if not manual and game_dir in self._malware_checked:
            return
        self._malware_checked.add(game_dir)
        paths = malware_scan.jar_files(MODS.path(game_dir))
        cache = os.path.join(os.path.dirname(app_config.CACHE_FILE), MALWARE_CACHE)
        if not paths and not manual:
            return
        self._run_task(malware_scan.scan_folder, (paths, cache, manual),
                       lambda result: self._on_malware_scanned(result, manual, game_dir),
                       lambda e: logging.warning(tr("Проверка на вирусы: {0}", e)),
                       tr("Проверяем моды на вирусы…") if manual else None)

    def _on_malware_scanned(self, result, manual: bool, game_dir: str):
        import malware_scan
        for finding in result.findings:
            logging.warning(tr("Проверка на вирусы: {0} — {1} ({2})", os.path.basename(finding.path), finding.detail,
                               finding.member))
        if result.system:
            choice = ask_choice(self, QMessageBox.Icon.Critical, tr("Компьютер заражён"), tr(
                "Найдены следы вируса fractureiser — он крадёт пароли, токены Discord и Microsoft:<br><br>{0}<br><br>"
                "Не запускайте Minecraft, пока не выполните инструкцию по очистке, и смените пароли с другого "
                "устройства.", "<br>".join(result.system)), [tr("Открыть инструкцию"), tr("Закрыть")], rich_text=True)
            if choice == 0:
                from PyQt6.QtCore import QUrl
                from PyQt6.QtGui import QDesktopServices
                QDesktopServices.openUrl(QUrl(malware_scan.INFO_URL))
        files = malware_scan.group_by_file(result.findings)
        if not files:
            if manual:
                self.toasts.show(tr("Проверено файлов: {0} — вирусов не найдено.", result.checked), "success")
            return
        if not result.malware and not manual:  # Только подозрительное — не прерываем, а предлагаем посмотреть
            self.toasts.show(tr("В модах есть подозрительный код: {0}.", len(files)), "warning", tr("Подробнее"),
                             lambda: self._show_malware(files, game_dir), timeout=15000)
            return
        self._show_malware(files, game_dir)

    def _show_malware(self, files: dict, game_dir: str):
        rows = []
        for path, items in files.items():
            danger = any(f.severity == "malware" for f in items)
            kind = tr("<b style='color:{0}'>вирус</b>", self.tokens["danger"]) if danger else tr("подозрительно")
            details = "; ".join(dict.fromkeys(f.detail for f in items))[:220]
            rows.append(f"• <b>{os.path.basename(path)}</b> — {kind}: {details}")
        malware = any(f.severity == "malware" for items in files.values() for f in items)
        intro = (tr("В этих модах найден код вируса fractureiser. Уберите их и не запускайте игру с ними.")
                 if malware else tr("В этих модах в коде спрятаны адреса — так делают вирусы, но иногда и обычные "
                                    "моды. Если мод скачан не с Modrinth или CurseForge, лучше его убрать."))
        choice = ask_choice(self, QMessageBox.Icon.Critical if malware else QMessageBox.Icon.Warning,
                            tr("Опасные моды") if malware else tr("Подозрительные моды"),
                            intro + "<br><br>" + "<br>".join(rows), [tr("Убрать в бэкап"), tr("Оставить")],
                            default=0 if malware else 1, rich_text=True)
        if choice == 0:
            self._quarantine(list(files), game_dir)

    def _quarantine(self, paths: List[str], game_dir: str):
        """Убирает файлы из папки модов в бэкап (вернуть можно из истории)."""
        if not self._confirm_game_not_running(paths):
            return
        transaction = UpdateTransaction(MODS.path(game_dir), "delete")
        errors = []
        for path in paths:
            try:
                transaction.backup_file(path)
            except OSError as e:
                errors.append(f"{os.path.basename(path)}: {e}")
        if errors:
            show_message(self, QMessageBox.Icon.Warning, tr("Не всё удалось"), "\n".join(errors[:10]))
        self.toasts.show(tr("Убрано в бэкап: {0}", len(paths) - len(errors)), "success", tr("Отменить"),
                         lambda: self._rollback_dir(transaction.dir), timeout=9000)
        self.sessions.pop(self._skey(content=MODS), None)
        if self.content is MODS:
            self.start_local_scan()

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
