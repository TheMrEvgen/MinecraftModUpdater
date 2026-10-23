"""Сборка: закрепление, выключение, метки, снимки, переход на другую версию игры, готовность.

Примесь к главному окну (ModUpdaterApp): методы работают с его атрибутами."""
import logging
import os
import webbrowser
from typing import Dict, List, Optional

from PyQt6.QtCore import QUrl, Qt
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox

from content import CONTENT_TYPES, MODS
from i18n import tr
from scanner import apply_install_info
from ui.dialogs import MigrationDialog, ask_text
from ui.widgets import make_action
from ui_components import confirm, show_message
from updater import UpdateTransaction, create_snapshot, delete_snapshot, prune_snapshots, restore_snapshot, set_disabled
from view_model import pin_key
from window.common import PAGE_HISTORY


class ModpackMixin:
    # ================================================================== Прочее

    def _open_path(self, path: str):
        if path and os.path.exists(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        else:
            self.toasts.show(tr("Папка не найдена."), "warning")

    def _show_in_folder(self, entry: dict):
        path = entry.get("filepath")
        if not path or not os.path.exists(path):
            return
        if os.name == "nt":
            import subprocess
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            self._open_path(os.path.dirname(path))

    def _open_mod_page(self, entry: dict):
        if entry.get("project_id"):
            kind = {"shader": "shader", "resourcepack": "resourcepack"}.get(entry.get("project_type"), "mod")
            webbrowser.open(f"https://modrinth.com/{kind}/{entry.get('slug') or entry['project_id']}")

    def _open_catalog_hit(self, hit: Optional[dict]):
        if hit:
            webbrowser.open(f"https://modrinth.com/{hit.get('project_type', 'mod')}/{hit.get('slug') or hit.get('project_id')}")

    # ================================================================== Закрепление, выключение, метки

    def _same_file_entries(self, path: str) -> List[dict]:
        norm = os.path.normcase(os.path.normpath(path))
        return [e for e in self._all_entries() if e.get("filepath") and os.path.normcase(os.path.normpath(e["filepath"])) == norm]

    def _toggle_pin(self, entry: dict):
        key = pin_key(entry)
        if not key:
            return
        pinned = self._profile().setdefault("pinned", {})
        self._push_undo(tr("закрепление"), lambda: self._toggle_pin(entry))
        if key in pinned:
            del pinned[key]
            self.toasts.show(tr('«{0}» снова обновляется.', entry.get('name')), "info")
        else:
            pinned[key] = entry.get("current_version") or ""
            self.toasts.show(tr('«{0}» закреплён: обновления для него не предлагаются.', entry.get('name')), "success",
                             tr("Отменить"), lambda: self._toggle_pin(entry))
        self._set_data(self.mods_data)
        self.save_config()

    def _toggle_disabled(self, entries: List[dict]):
        entries = [e for e in entries if e.get("filepath")]
        if not entries or self._busy:
            return
        disable = not all(e.get("disabled") for e in entries)
        if not self._confirm_game_not_running([e["filepath"] for e in entries]):
            return
        changed, errors = [], []
        for entry in entries:
            if bool(entry.get("disabled")) == disable:
                continue
            old_path = entry["filepath"]
            try:
                new_path = set_disabled(old_path, disable)
            except OSError as e:
                errors.append(f"{entry.get('name')}: {e}")
                continue
            for same in self._same_file_entries(old_path):
                same["filepath"], same["disabled"] = new_path, disable
                same["_is_checked"] = False
            changed.append(entry)
        if changed:
            self._push_undo(tr("выключение") if disable else tr("включение"), lambda: self._toggle_disabled(changed))
            self._set_data(self.mods_data)
            self.sessions[self._skey()] = self.mods_data
            what = f"«{changed[0].get('name')}»" if len(changed) == 1 else tr('{0} модов', len(changed))
            self.toasts.show(f"{tr('Выключено') if disable else tr('Включено')}: {what}", "success", tr("Отменить"),
                             lambda: self._toggle_disabled(changed), timeout=8000)
        if errors:
            show_message(self, QMessageBox.Icon.Warning, tr("Не удалось переименовать"), "\n".join(errors[:10]))

    def _show_tags_menu(self, entry: dict, pos):
        key = pin_key(entry)
        if not key:
            return
        t = self.tokens
        menu = QMenu(self)
        menu.addAction(make_action(menu, t, "plus", tr("Добавить метку…"), lambda: self._add_tag(entry)))
        own = self.mod_tags.get(key, [])
        if own:
            menu.addSeparator()
        for tag in own:
            menu.addAction(make_action(menu, t, "x", tr('Убрать «{0}»', tag), lambda _=False, tg=tag: self._remove_tag(entry, tg)))
        menu.exec(pos)

    def _add_tag(self, entry: dict):
        existing = sorted({tag for tags in self.mod_tags.values() for tag in tags})
        hint = tr("Например: интерфейс, для сервера, графика") + (tr('\nУже есть: {0}', ', '.join(existing[:8])) if existing else "")
        tag = ask_text(self, tr("Новая метка"), hint, tr("Название метки"))
        if not tag:
            return
        tags = self.mod_tags.setdefault(pin_key(entry), [])
        if tag.lower() not in (t.lower() for t in tags):
            tags.append(tag)
            self._push_undo(tr('метка «{0}»', tag), lambda: self._remove_tag(entry, tag))
        self._after_tags_changed(entry)

    def _remove_tag(self, entry: dict, tag: str):
        key = pin_key(entry)
        tags = [t for t in self.mod_tags.get(key, []) if t != tag]
        if tags:
            self.mod_tags[key] = tags
        else:
            self.mod_tags.pop(key, None)
        self._after_tags_changed(entry)

    def _after_tags_changed(self, entry: dict):
        self._refresh_tag_combo()
        self.proxy.invalidateFilter()
        self._refresh_empty_state()
        if self.details.entry is entry:
            self.details.set_entry(entry, self._icon_for_entry(entry))
        self.save_config()

    # ================================================================== Снимки сборки

    def _auto_snapshot(self, reason: str):
        if self.settings.get("auto_snapshot", True) and self.mods_path:
            self._create_snapshot(reason, quiet=True)

    def _create_snapshot(self, reason: str = "manual", quiet: bool = False):
        if not self.mods_path or not os.path.isdir(self.mods_path):
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            snapshot = create_snapshot(self.mods_path, reason, self.content)
            if reason != "manual":
                prune_snapshots(self.mods_path, int(self.settings.get("snapshots_keep", 10)))
        except OSError as e:
            logging.error(tr('Не удалось создать снимок: {0}', e))
            snapshot = None
            if not quiet:
                show_message(self, QMessageBox.Icon.Warning, tr("Снимок не создан"), str(e))
        finally:
            QApplication.restoreOverrideCursor()
        if snapshot and not quiet:
            self.toasts.show(tr('Снимок сохранён: {0} файлов', len(snapshot['files'])), "success", tr("История"),
                             lambda: self._switch_page(PAGE_HISTORY))
        if self.stack.currentIndex() == PAGE_HISTORY:
            self._refresh_history()

    def _restore_snapshot(self, snapshot: dict):
        if self._busy:
            self.toasts.show(tr("Дождитесь окончания текущей операции."), "warning")
            return
        count = len(snapshot.get("files") or [])
        if not confirm(self, tr("Восстановить снимок"),
                       tr('Вернуть папку {0} к снимку от {1} ({2} файлов)?\nТекущие файлы уйдут в бэкап — восстановление можно отменить.', self.content.folder, snapshot.get('created', '').replace('T', ' '), count),
                       tr("Восстановить"), tr("Отмена")):
            return
        files = [os.path.join(self.mods_path, f) for f in os.listdir(self.mods_path) if self.content.matches(f)]
        if not self._confirm_game_not_running(files):
            return
        transaction, errors = restore_snapshot(self.mods_path, snapshot, self.content)
        if errors:
            show_message(self, QMessageBox.Icon.Warning, tr("Восстановлено частично"), "\n".join(errors[:10]))
        else:
            self.toasts.show(tr("Сборка восстановлена из снимка."), "success", tr("Отменить"),
                             lambda: self._rollback_dir(transaction.dir), timeout=9000)
        self.sessions.pop(self._skey(), None)
        self._refresh_history()
        self.start_local_scan()

    def _delete_snapshot(self, snapshot: dict):
        if confirm(self, tr("Удалить снимок"), tr("Удалить этот снимок? Файлы текущей сборки не изменятся."), tr("Удалить"), tr("Отмена")):
            delete_snapshot(snapshot)
            self._refresh_history()

    def _invalidate_folder(self, folder: str):
        """Забывает сохранённые списки, если их папка изменилась."""
        game_dir = self._profile().get("mods_path", "")
        for content in CONTENT_TYPES:
            if os.path.normcase(content.path(game_dir)) == os.path.normcase(folder or ""):
                self.sessions.pop(self._skey(content=content), None)

    # ================================================================== Переход на другую версию Minecraft

    def migrate_game_version(self, target: Optional[str] = None):
        if self._busy or not self.mods_data or self.content is not MODS or self.is_import_mode:
            return
        entries = [e for e in self._count_items if e.get("filepath")]
        dialog = MigrationDialog(self, self.tokens, entries, self.game_version, self.loader, self._channel(),
                                 self._run_task, self.content, target if isinstance(target, str) else None)
        if not dialog.exec() or not dialog.results:
            return
        target = dialog.target
        ready = [r for r in dialog.results if r["version"]]
        missing = [r["entry"] for r in dialog.results if not r["version"]]
        if not self._confirm_game_not_running([e.get("filepath") for e in entries]):
            return
        self._auto_snapshot("migrate")
        # Версия, которая уже стоит и подходит под новую версию игры, не скачивается повторно
        to_install = [r["entry"] for r in ready
                      if r["version"].get("id") != (r["entry"].get("installed_info") or {}).get("id")
                      and apply_install_info(r["entry"], r["version"])]
        if dialog.disable_missing.isChecked():
            for entry in missing:
                if entry.get("disabled"):
                    continue
                old_path = entry["filepath"]
                try:
                    new_path = set_disabled(old_path, True)
                except OSError as e:
                    logging.error(tr('Не удалось выключить {0}: {1}', entry.get('name'), e))
                    continue
                for same in self._same_file_entries(old_path):
                    same["filepath"], same["disabled"] = new_path, True
        old_version = self.game_version
        self._profile()["game_version"] = target
        self.game_version = target
        self._update_header()
        self._refresh_sidebar()
        self.start_update(to_install, UpdateTransaction(self.mods_path, "migrate"))
        show_message(self, QMessageBox.Icon.Information, tr("Не забудьте про лаунчер"),
                     tr('Моды обновляются под Minecraft {0}.\n\nСмените версию игры и загрузчика на {1} в лаунчере (в Prism: «Изменить» → «Версия»). Вернуть всё как было можно на странице «История» — там сохранён снимок сборки под {2}.', target, target, old_version))

    # ================================================================== Готовность к новой версии Minecraft

    def _maybe_check_readiness(self, force: bool = False):
        """После проверки обновлений: вышла ли новая версия игры и сколько модов уже готовы к ней."""
        import readiness
        if self.content is not MODS or self.is_import_mode or not self.game_version or not self.loader:
            return
        profile_name = self.current_profile_name
        saved = self._profile().get("readiness")
        if not force and not readiness.needs_check(saved):
            self._show_readiness()
            return
        entries = [e for e in self._count_items if e.get("filepath")]
        self._run_task(readiness.check_readiness, (entries, self.game_version, self.loader, self._channel()),
                       lambda result: self._on_readiness(profile_name, result),
                       lambda e: logging.info(tr('Проверка готовности к новой версии не удалась: {0}', e)), None)

    def _on_readiness(self, profile_name: str, result: Optional[Dict]):
        profile = self.profiles.get(profile_name)
        if profile is None:
            return
        previous = profile.get("readiness") or {}
        if result is None:
            profile.pop("readiness", None)
        else:
            same_target = previous.get("target") == result["target"]
            result["dismissed"] = previous.get("dismissed", False) and same_target
            result["notified_all"] = previous.get("notified_all", False) and same_target
            if result["total"] and result["ready"] == result["total"] and not result["notified_all"]:
                result["notified_all"] = True
                result["dismissed"] = False
                self.toasts.show(tr('Все моды сборки «{0}» готовы к Minecraft {1}!', profile_name, result['target']), "success",
                                 tr("Перейти"), lambda t=result["target"]: self.migrate_game_version(t), timeout=15000)
                QApplication.alert(self)
            elif not same_target:
                self.toasts.show(tr('Вышла Minecraft {0}: готово {1} из {2} модов.', result['target'], result['ready'], result['total']),
                                 "info", tr("Подробнее"), lambda t=result["target"]: self.migrate_game_version(t), timeout=10000)
            profile["readiness"] = result
        self.save_config()
        if profile_name == self.current_profile_name:
            self._show_readiness()

    def _show_readiness(self):
        data = self._profile().get("readiness")
        visible = bool(data) and not data.get("dismissed") and self.content is MODS and not self.is_import_mode \
            and data.get("target") != self.game_version
        if visible:
            t = self.tokens
            ready, total = data["ready"], data["total"]
            color = t["accent"] if ready == total else t["warning"]
            missing = data.get("missing") or []
            rest = tr(' Ещё не готовы: {0}', ', '.join(missing[:4])) + (tr(' и ещё {0}', len(missing) - 4) if len(missing) > 4 else "") \
                if missing else tr(" Можно переходить.")
            self.readiness_text.setText(tr("<b>Вышла Minecraft {0}</b> — готово <span style='color:{1}'><b>{2} из {3}</b></span> модов.{4}", data['target'], color, ready, total, rest))
        self.readiness_banner.setVisible(visible)

    def _dismiss_readiness(self):
        data = self._profile().get("readiness")
        if data:
            data["dismissed"] = True
            self.save_config()
        self.readiness_banner.hide()
