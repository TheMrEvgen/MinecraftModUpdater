"""
Возможности главного окна, вынесенные из main_window.py, чтобы тот оставался обозримым:
избранное и заметки, отмена действий, массовые действия, «новое с прошлого раза», группы, клонирование
и сравнение сборок, наборы модов, поиск проблемного мода, разбор краша, целостность, бэкапы, перенос
настроек, статистика, установка из каталога, режим «Минимум», горячие клавиши, подсказки поиска.

Класс-примесь: методы работают с атрибутами ModUpdaterApp.
"""
import copy
import logging
import math
import os
import sys
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import QProcess, QStringListModel, Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import QApplication, QCompleter, QFileDialog, QMenu, QMessageBox

import config as app_config
from bisect_session import BisectSession
from content import CONTENT_TYPES, MODS, enabled_name
from crash_analysis import find_crash_files
from enums import ModStatus
from maintenance import (
    backups_usage, check_integrity, clone_game_dir, compare_entries, enabled_keys, preset_key, preset_plan,
    prune_backups,
)
from query import QUERY_HINTS, is_abandoned
from scanner import resolve_many_installs, resolve_project_install, scan_local
from ui.dialogs import ask_text
from ui.shortcuts import effective_shortcuts
from ui.tools_dialogs import CloneDialog, CompareDialog, CrashDialog, IntegrityDialog
from ui.widgets import make_action
from ui_components import ask_choice, confirm, show_message
from updater import UpdateTransaction, set_disabled
from view_model import collect_tags, iter_all_entries, pin_key

UNDO_LIMIT = 30


class ToolsMixin:
    # ================================================================== Отмена (Ctrl+Z)

    def _push_undo(self, description: str, action: Callable[[], None]):
        """Запоминает, как отменить действие. Во время самой отмены новые записи не добавляются."""
        if getattr(self, "_undoing", False):
            return
        self._undo.append((description, action))
        del self._undo[:-UNDO_LIMIT]

    def _undo_last(self):
        if self._busy:
            self.toasts.show("Дождитесь окончания текущей операции.", "warning")
            return
        if not self._undo:
            self.toasts.show("Отменять нечего.", "info")
            return
        description, action = self._undo.pop()
        self._undoing = True
        try:
            action()
        finally:
            self._undoing = False
        self.toasts.show(f"Отменено: {description}", "success")

    # ================================================================== Избранное и заметки

    def _apply_personal_flags(self, mods_data: List[dict]):
        """Отметки пользователя на записях: избранное и заметка (их видят поиск, сортировка и строка)."""
        favorites = set(self.favorites)
        for entry in iter_all_entries(mods_data):
            key = pin_key(entry)
            entry["_favorite"] = bool(key) and key in favorites
            entry["_note"] = self.mod_notes.get(key, "") if key else ""

    def _toggle_favorite(self, entries: List[dict]):
        entries = [e for e in entries if pin_key(e)]
        if not entries:
            return
        make_favorite = not all(e.get("_favorite") for e in entries)
        keys = {pin_key(e) for e in entries}
        before = list(self.favorites)
        if make_favorite:
            self.favorites.extend(k for k in keys if k not in self.favorites)
        else:
            self.favorites[:] = [k for k in self.favorites if k not in keys]
        self._push_undo("избранное", lambda: self._restore_favorites(before))
        self._after_personal_change(entries)
        what = f"«{entries[0].get('name')}»" if len(entries) == 1 else f"{len(entries)} модов"
        self.toasts.show(f"{what} {'в избранном' if make_favorite else 'убран из избранного'}", "success")

    def _restore_favorites(self, favorites: List[str]):
        self.favorites[:] = favorites
        self._after_personal_change([])

    def _set_note(self, entry: dict, text: str):
        key = pin_key(entry)
        if not key or (self.mod_notes.get(key, "") == text):
            return
        if text:
            self.mod_notes[key] = text
        else:
            self.mod_notes.pop(key, None)
        for same in self._all_entries():
            if pin_key(same) == key:
                same["_note"] = text
        self._refresh_project(entry.get("project_id"))
        self.proxy.invalidateFilter()
        self.save_config()

    def _edit_note_current(self):
        entry = self._current_entry()
        if not entry:
            return
        self._select_entry(entry)
        self._open_current_details()
        self.details.note_edit.setFocus()

    def _after_personal_change(self, entries: List[dict]):
        self._apply_personal_flags(self.mods_data)
        self.proxy.invalidate()
        self.proxy.sort(0)
        self.view.viewport().update()
        if self.details.entry is not None:
            self.details.set_entry(self.details.entry, self._icon_for_entry(self.details.entry))
        self.save_config()

    def _current_entry(self) -> Optional[dict]:
        from ui.mod_list import entry_at
        return self.details.entry if self.details.isVisible() and self.details.entry else entry_at(self.view.currentIndex())

    # ================================================================== Массовые действия

    def _show_bulk_menu(self, pos):
        checked = self._checked_entries()
        files = [e for e in checked if e.get("filepath")]
        if not checked:
            return
        t = self.tokens
        menu = QMenu(self)
        all_pinned = bool(files) and all(e.get("_pinned") for e in files)
        all_fav = all(e.get("_favorite") for e in checked)
        if files:
            menu.addAction(make_action(menu, t, "unlock" if all_pinned else "lock",
                                       "Открепить" if all_pinned else "Закрепить версии",
                                       lambda: self._set_pins(files, not all_pinned)))
        menu.addAction(make_action(menu, t, "star", "Убрать из избранного" if all_fav else "В избранное",
                                   lambda: self._toggle_favorite(checked)))
        menu.addAction(make_action(menu, t, "tag", "Добавить метку…", lambda: self._add_tag_many(checked)))
        if files:
            menu.addAction(make_action(menu, t, "camera", "Сохранить как набор модов…",
                                       lambda: self._save_preset(files)))
        menu.exec(pos)

    def _set_pins(self, entries: List[dict], pinned: bool):
        pins = self._profile().setdefault("pinned", {})
        before = dict(pins)
        for entry in entries:
            key = pin_key(entry)
            if pinned:
                pins[key] = entry.get("current_version") or ""
            else:
                pins.pop(key, None)
        self._push_undo("закрепление", lambda: self._restore_pins(before))
        self._set_data(self.mods_data)
        self.save_config()
        self.toasts.show(f"{'Закреплено' if pinned else 'Откреплено'}: {len(entries)}", "success")

    def _restore_pins(self, pins: Dict[str, str]):
        profile_pins = self._profile().setdefault("pinned", {})
        profile_pins.clear()
        profile_pins.update(pins)
        self._set_data(self.mods_data)
        self.save_config()

    def _add_tag_many(self, entries: List[dict]):
        tag = ask_text(self, "Новая метка", f"Метка для {len(entries)} модов", "Название метки")
        if not tag:
            return
        before = copy.deepcopy(self.mod_tags)
        for entry in entries:
            tags = self.mod_tags.setdefault(pin_key(entry), [])
            if tag.lower() not in (t.lower() for t in tags):
                tags.append(tag)
        self._push_undo("метки", lambda: self._restore_tags(before))
        self._after_tags_changed(entries[0])

    def _restore_tags(self, tags: Dict[str, List[str]]):
        self.mod_tags.clear()
        self.mod_tags.update(tags)
        self._after_tags_changed(self.details.entry or {})

    # ================================================================== Новое с прошлого раза

    def _mark_new_updates(self, mods_data: List[dict]) -> List[dict]:
        """Помечает обновления, которых не было при прошлом запуске программы, и запоминает текущие."""
        profile = self.current_profile_name
        baseline = self._seen_baseline.get(profile)
        current = {}
        fresh = []
        for entry in iter_all_entries(mods_data):
            pid, version = entry.get("project_id"), entry.get("new_version")
            if not pid or not version or entry.get("status") != ModStatus.UPDATE_AVAILABLE:
                continue
            current[pid] = version
            is_new = baseline is not None and baseline.get(pid) != version
            entry["_new"] = is_new
            if is_new and entry not in fresh and not entry.get("_pinned"):
                fresh.append(entry)
        self.last_seen[profile] = current
        if baseline is None:  # Первый запуск: всё «старое», запоминаем как точку отсчёта
            self._seen_baseline[profile] = dict(current)
        return fresh

    # ================================================================== Сборки: порядок, группы, клон, сравнение

    def _profile_groups(self) -> List[str]:
        return sorted({p.get("group") or "" for p in self.profiles.values()} - {""})

    def _on_profiles_reordered(self, order: List[tuple]):
        if len(order) != len(self.profiles):
            return
        self.profiles = {name: {**self.profiles[name], "group": group} for name, group in order if name in self.profiles}
        self._refresh_sidebar()
        self.save_config()

    def _set_profile_group(self, name: str):
        groups = self._profile_groups()
        hint = "Пусто — без группы" + (f"\nУже есть: {', '.join(groups)}" if groups else "")
        group = ask_text(self, "Группа сборки", hint, "Например: Серверы", self.profiles[name].get("group") or "")
        if group is None and not self.profiles[name].get("group"):
            return
        self.profiles[name]["group"] = group or ""
        self._refresh_sidebar()
        self.save_config()

    def _clone_profile(self, name: str):
        profile = self.profiles.get(name)
        if not profile or not profile.get("mods_path") or self._busy:
            return
        dialog = CloneDialog(self, name, profile["mods_path"], self.profiles.keys())
        if not dialog.exec():
            return
        new_name, target, with_saves = dialog.values()

        def done(copied):
            versions = {"game_version": self.game_version, "loader": self.loader} if name == self.current_profile_name else {}
            self.profiles[new_name] = {**profile, **versions, "mods_path": target, "source": "manual",
                                       "manual_mode": True, "pinned": dict(profile.get("pinned") or {})}
            self._refresh_sidebar()
            self.save_config()
            self.toasts.show(f"Сборка «{new_name}» создана: скопировано {', '.join(copied)}.", "success",
                             "Сравнить", lambda: self._compare_profiles(name, new_name), timeout=9000)

        self._run_task(clone_game_dir, (profile["mods_path"], target, with_saves), done,
                       lambda e: show_message(self, QMessageBox.Icon.Critical, "Не удалось клонировать", e),
                       "Копируем сборку…")

    def _profile_entries_task(self, name: str) -> List[dict]:
        """Записи модов сборки: из памяти или быстрым локальным сканированием (из потока)."""
        data = self.sessions.get(self._skey(name, MODS))
        if data is None:
            data = scan_local(MODS.path(self.profiles[name].get("mods_path", "")), app_config.CACHE_FILE, MODS)
        return [e for e in iter_all_entries(data) if e.get("filepath")]

    def _compare_profiles(self, left: str, right: str):
        if left not in self.profiles or right not in self.profiles:
            return

        def collect():
            return compare_entries(self._profile_entries_task(left), self._profile_entries_task(right))

        self._run_task(collect, (), lambda diff: CompareDialog(self, self.tokens, left, right, diff).exec(),
                       lambda e: show_message(self, QMessageBox.Icon.Warning, "Сравнение", e), "Сравниваем сборки…")

    # ================================================================== Наборы модов

    def _presets(self) -> Dict[str, List[str]]:
        return self._profile().setdefault("presets", {})

    def _refresh_presets(self):
        presets = self._presets()
        current = enabled_keys(self._count_items) if self.content is MODS else []
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        active = next((name for name, keys in presets.items() if sorted(keys) == current), None)
        self.preset_combo.addItem("Набор: свой" if not active else f"Набор: {active}", None)
        for name in presets:
            self.preset_combo.addItem(name, name)
        self.preset_combo.setCurrentIndex(0)
        self.preset_combo.blockSignals(False)
        visible = self.content is MODS and not self.is_import_mode and bool(self._count_items)
        self.preset_combo.setVisible(visible and bool(presets))
        self.preset_btn.setVisible(visible)

    def _show_preset_menu(self):
        t = self.tokens
        menu = QMenu(self)
        menu.addAction(make_action(menu, t, "camera", "Сохранить включённые моды как набор…",
                                   lambda: self._save_preset([e for e in self._count_items if e.get("filepath")])))
        presets = self._presets()
        if presets:
            menu.addSeparator()
            for name in presets:
                sub = menu.addMenu(name)
                sub.addAction(make_action(sub, t, "check", "Применить", lambda _=False, n=name: self._apply_preset(n)))
                sub.addAction(make_action(sub, t, "refresh", "Перезаписать текущими",
                                          lambda _=False, n=name: self._overwrite_preset(n)))
                sub.addAction(make_action(sub, t, "trash", "Удалить", lambda _=False, n=name: self._delete_preset(n), "danger"))
        menu.exec(self.preset_btn.mapToGlobal(self.preset_btn.rect().bottomLeft()))

    def _save_preset(self, entries: List[dict]):
        name = ask_text(self, "Новый набор", "Название набора — например, «Для записи видео» или «Слабый ПК».\n"
                        "В набор попадут моды, которые сейчас включены.", "Название")
        if not name:
            return
        keys = enabled_keys(entries)
        self._presets()[name] = keys
        self._refresh_presets()
        self.save_config()
        self.toasts.show(f"Набор «{name}» сохранён: {len(keys)} модов.", "success")

    def _overwrite_preset(self, name: str):
        self._presets()[name] = enabled_keys(self._count_items)
        self._refresh_presets()
        self.save_config()
        self.toasts.show(f"Набор «{name}» обновлён.", "success")

    def _delete_preset(self, name: str):
        if confirm(self, "Удалить набор", f"Удалить набор «{name}»? Моды не изменятся.", "Удалить", "Отмена"):
            self._presets().pop(name, None)
            self._refresh_presets()
            self.save_config()

    def _apply_preset(self, name: str):
        keys = self._presets().get(name)
        if keys is None or self._busy:
            return
        plan = preset_plan(self._count_items, keys)
        if not plan["enable"] and not plan["disable"]:
            self.toasts.show(f"Набор «{name}» уже применён.", "info")
            return
        changed = plan["enable"] + plan["disable"]
        if not self._confirm_game_not_running([e["filepath"] for e in changed]):
            return
        before = enabled_keys(self._count_items)
        errors = self._rename_entries(plan["disable"], True) + self._rename_entries(plan["enable"], False)
        self._set_data(self.mods_data)
        self.sessions[self._skey()] = self.mods_data
        self._push_undo(f"набор «{name}»", lambda: self._apply_keys(before))
        self.toasts.show(f"Набор «{name}»: включено {len(plan['enable'])}, выключено {len(plan['disable'])}.", "success",
                         "Отменить", self._undo_last, timeout=8000)
        if errors:
            show_message(self, QMessageBox.Icon.Warning, "Не всё переключилось", "\n".join(errors[:10]))

    def _apply_keys(self, keys: List[str]):
        plan = preset_plan(self._count_items, keys)
        self._rename_entries(plan["disable"], True)
        self._rename_entries(plan["enable"], False)
        self._set_data(self.mods_data)
        self.sessions[self._skey()] = self.mods_data

    def _rename_entries(self, entries: List[dict], disable: bool) -> List[str]:
        """Выключает/включает файлы и обновляет все записи с этим файлом. Возвращает ошибки."""
        errors = []
        for entry in entries:
            old_path = entry["filepath"]
            try:
                new_path = set_disabled(old_path, disable)
            except OSError as e:
                errors.append(f"{entry.get('name')}: {e}")
                continue
            for same in self._same_file_entries(old_path):
                same["filepath"], same["disabled"] = new_path, disable
        return errors

    # ================================================================== Поиск проблемного мода

    def _installed_mods(self) -> List[dict]:
        data = self.mods_data if self.content is MODS else self.sessions.get(self._skey(content=MODS)) or []
        unique = {}
        for entry in iter_all_entries(data):
            if entry.get("filepath"):
                unique.setdefault(os.path.normcase(entry["filepath"]), entry)
        return list(unique.values())

    def _start_bisect(self):
        if self._busy or self._profile().get("bisect"):
            return
        if self.content is not MODS:
            self._set_content(MODS)
        mods = self._installed_mods()
        enabled = [e for e in mods if not e.get("disabled")]
        if len(enabled) < 2:
            self.toasts.show("Для поиска нужно хотя бы два включённых мода.", "warning")
            return
        steps = max(1, math.ceil(math.log2(len(enabled))))
        if not confirm(self, "Поиск проблемного мода",
                       "Программа будет по очереди выключать половину модов, а вы — запускать игру и отвечать, "
                       f"осталась ли проблема. Понадобится около {steps} запусков игры.\n\n"
                       "Моды, которые нужны выключенным, выключаются вместе с ними — ложных ошибок из-за "
                       "зависимостей не будет. В конце всё вернётся как было. Начать?", "Начать", "Отмена"):
            return
        name_of = {e["filepath"]: enabled_name(os.path.basename(e["filepath"])) for e in mods}
        by_pid = {e.get("project_id"): name_of[e["filepath"]] for e in mods if e.get("project_id")}
        requires: Dict[str, set] = {}
        for entry in iter_all_entries(self.mods_data):
            if not entry.get("filepath"):
                continue
            for dep in entry.get("dependencies") or []:
                if dep.get("type") == "required" and by_pid.get(dep.get("project_id")):
                    requires.setdefault(name_of[entry["filepath"]], set()).add(by_pid[dep["project_id"]])
        session = BisectSession.start([name_of[e["filepath"]] for e in enabled],
                                      [name_of[e["filepath"]] for e in mods if e.get("disabled")], requires)
        if not self._confirm_game_not_running([e["filepath"] for e in mods]):
            return
        self._profile()["bisect"] = session.to_dict()
        self.save_config()
        self._apply_bisect_state(session)

    def _bisect_session(self) -> Optional[BisectSession]:
        data = self._profile().get("bisect")
        try:
            return BisectSession.from_dict(data) if data else None
        except (KeyError, TypeError):
            self._profile().pop("bisect", None)
            return None

    def _set_disabled_names(self, disabled_names: set):
        """Приводит папку к состоянию: выключены ровно эти моды (по имени файла без .disabled)."""
        mods = self._installed_mods()
        to_disable = [e for e in mods if not e.get("disabled") and enabled_name(os.path.basename(e["filepath"])) in disabled_names]
        to_enable = [e for e in mods if e.get("disabled") and enabled_name(os.path.basename(e["filepath"])) not in disabled_names]
        errors = self._rename_entries(to_disable, True) + self._rename_entries(to_enable, False)
        self._set_data(self.mods_data)
        self.sessions[self._skey(content=MODS)] = self.mods_data
        if errors:
            show_message(self, QMessageBox.Icon.Warning, "Не всё переключилось",
                         "Закройте игру и повторите.\n\n" + "\n".join(errors[:10]))

    def _apply_bisect_state(self, session: BisectSession):
        if session.done:
            self._set_disabled_names(set(session.original_disabled))
            culprit = session.culprit
            entry = next((e for e in self._installed_mods()
                          if enabled_name(os.path.basename(e["filepath"])) == culprit), None)
            self.bisect_banner.show_result(entry.get("name") if entry else culprit)
            if entry:
                self._select_entry(entry)
            return
        self._set_disabled_names(set(session.original_disabled) | set(session.disabled_now))
        self.bisect_banner.show_step(session.step + 1, session.step + session.steps_left(), len(session.disabled_now),
                                     len(session.suspects))

    def _on_bisect_answer(self, problem_persists: bool):
        session = self._bisect_session()
        if not session:
            self.bisect_banner.hide()
            return
        session.answer(problem_persists)
        self._profile()["bisect"] = session.to_dict()
        self.save_config()
        self._apply_bisect_state(session)

    def _finish_bisect(self, keep_culprit_disabled: bool):
        session = self._bisect_session()
        self._profile().pop("bisect", None)
        self.save_config()
        self.bisect_banner.hide()
        if not session:
            return
        disabled = set(session.original_disabled)
        if keep_culprit_disabled and session.culprit:
            disabled.add(session.culprit)
        self._set_disabled_names(disabled)
        self.toasts.show("Поиск завершён: " + (f"«{session.culprit}» выключен, остальное вернулось как было."
                                               if keep_culprit_disabled else "все моды вернулись как были."), "success")

    def _cancel_bisect(self):
        if confirm(self, "Прервать поиск", "Вернуть все моды в состояние до начала поиска?", "Вернуть", "Продолжить поиск"):
            self._finish_bisect(False)

    def _restore_bisect_banner(self):
        session = self._bisect_session() if self.content is MODS else None
        if not session:
            self.bisect_banner.hide()
        elif session.done:
            self.bisect_banner.show_result(session.culprit)
        else:
            self.bisect_banner.show_step(session.step + 1, session.step + session.steps_left(),
                                         len(session.disabled_now), len(session.suspects))

    # ================================================================== Разбор краша

    def _show_crash_dialog(self):
        game_dir = self._profile().get("mods_path", "")
        if not game_dir:
            return
        dialog = CrashDialog(self, self.tokens, find_crash_files(game_dir), self._installed_mods(), self._run_task)
        dialog.disable_requested.connect(lambda e: self._toggle_disabled([e]))
        dialog.show_requested.connect(self._show_entry_in_list)
        dialog.bisect_requested.connect(self._start_bisect)
        dialog.exec()

    def _show_entry_in_list(self, entry: dict):
        self._switch_page(self.PAGE_MODS_INDEX)
        if self.content is not MODS:
            self._set_content(MODS)
        self._reset_filters()
        self._select_entry(entry)
        self._open_current_details()

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
        self.toasts.show(f"Убрано в бэкап: {len(paths) - len(errors)}", "success", "Отменить",
                         lambda: self._rollback_dir(transaction.dir), timeout=9000)
        if errors:
            show_message(self, QMessageBox.Icon.Warning, "Не всё удалось", "\n".join(errors[:10]))
        self.sessions.pop(self._skey(), None)
        self.start_local_scan()

    def _backup_usage_text(self) -> str:
        if not self.mods_path or not os.path.isdir(self.mods_path):
            return ""
        from view_model import format_size
        usage = backups_usage(self.mods_path)
        total = sum(usage.values())
        return (f"Бэкапы папки {self.content.folder} занимают {format_size(total)} "
                f"(операции {format_size(usage['transactions'])}, снимки {format_size(usage['snapshots'])}).")

    def _prune_backups(self, manual: bool = False):
        if not self.mods_path or not os.path.isdir(self.mods_path):
            return
        days = int(self.settings.get("backup_max_days", 30)) or None
        megabytes = int(self.settings.get("backup_max_mb", 1024))
        try:
            removed = prune_backups(self.mods_path, days, megabytes * 1024 * 1024 if megabytes else None)
        except OSError as e:
            logging.error(f"Очистка бэкапов: {e}")
            removed = 0
        if manual:
            self.toasts.show(f"Удалено старых бэкапов: {removed}." if removed else "Нечего удалять — всё в пределах лимитов.",
                             "success" if removed else "info")
            self.settings_page.set_backup_usage(self._backup_usage_text())

    # ================================================================== Перенос настроек

    def _config_snapshot(self) -> dict:
        return {"theme": self.config_theme, "current_profile": self.current_profile_name, "profiles": self.profiles,
                "settings": self.settings, "hidden_instances": self.hidden_instances, "window": self.window_cfg,
                "mod_tags": self.mod_tags, "mod_notes": self.mod_notes, "favorites": self.favorites,
                "last_seen": self.last_seen}

    def _export_settings(self):
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт настроек", "mod-updater-settings.json", "JSON (*.json)")
        if not path:
            return
        try:
            app_config.export_config(self._config_snapshot(), path)
            self.toasts.show(f"Настройки сохранены в {os.path.basename(path)}", "success")
        except OSError as e:
            show_message(self, QMessageBox.Icon.Critical, "Ошибка", f"Не удалось сохранить файл:\n{e}")

    def _import_settings(self):
        if self._busy:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Импорт настроек", "", "JSON (*.json)")
        if not path:
            return
        try:
            imported = app_config.import_config(path)
        except ValueError as e:
            show_message(self, QMessageBox.Icon.Warning, "Импорт настроек", str(e))
            return
        choice = ask_choice(self, QMessageBox.Icon.Question, "Импорт настроек",
                            f"В файле {len(imported['profiles'])} сборок, {len(imported['mod_notes'])} заметок, "
                            f"{len(imported['favorites'])} избранных.\n\nЗаменить текущие настройки или добавить сборки "
                            "и заметки к имеющимся?", ["Добавить", "Заменить всё", "Отмена"], default=0)
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
                self.profiles.setdefault(name if name not in self.profiles else f"{name} (импорт)", profile)
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
        self.toasts.show("Настройки импортированы.", "success")

    # ================================================================== Статистика и замена заброшенных модов

    def _refresh_stats(self):
        self.stats_page.set_data(self._count_items, self.current_profile_name, self.mod_tags)

    def _stats_filter(self, query: str):
        self._switch_page(self.PAGE_MODS_INDEX)
        self._set_category_all_silent()
        self.search.setText(query)
        self._apply_filter(query=query)

    def _set_category_all_silent(self):
        from view_model import CAT_ALL
        self.cards[CAT_ALL].setChecked(True)
        self.proxy.set_filter(category=CAT_ALL)

    def _find_replacement(self, entry: dict):
        self._switch_page(self.PAGE_CATALOG_INDEX)
        self.catalog_page.tab_group.buttons()[CONTENT_TYPES.index(self.content)].setChecked(True)
        self.catalog_page.content = self.content
        self.catalog_page.search_like(entry)

    # ================================================================== Каталог: массовая установка и выбранная версия

    def _install_many_from_catalog(self, hits: List[dict]):
        if self._busy or not hits:
            if self._busy:
                self.toasts.show("Дождитесь окончания текущей операции.", "warning")
            return
        content = self.catalog_page.content
        installed = self._installed_ids().get(content.key, set())
        ids = [h.get("project_id") for h in hits]
        self.catalog_page.set_installing(ids, True)

        def resolved(result):
            entries, missing = result
            self.catalog_page.set_installing(ids, False)
            if missing:
                titles = [h.get("title") for h in hits if h.get("project_id") in missing]
                self.toasts.show(f"Нет версии для вашей сборки: {', '.join(titles)}", "warning")
            if entries:
                self._start_catalog_install(entries, content)

        self._run_task(resolve_many_installs, (ids, self.game_version, self.loader, self._channel(), installed, content),
                       resolved, lambda e: (self.catalog_page.set_installing(ids, False),
                                            self.toasts.show(f"Не удалось подготовить установку: {e}", "error")), None)

    def _install_catalog_version(self, hit: dict, version: dict):
        if self._busy:
            self.toasts.show("Дождитесь окончания текущей операции.", "warning")
            return
        content = self.catalog_page.content
        installed = self._installed_ids().get(content.key, set())
        pid = hit.get("project_id")
        self.catalog_page.set_installing([pid], True)
        self._run_task(resolve_project_install,
                       (pid, self.game_version, self.loader, self._channel(), installed, content, 3, version),
                       lambda entries: self._on_catalog_resolved(hit, content, entries),
                       lambda e: self._on_catalog_failed(pid, e), None)

    def _start_catalog_install(self, entries: List[dict], content):
        folder = content.path(self._profile().get("mods_path", ""))
        self._catalog_installing = [e["project_id"] for e in entries]
        self._catalog_content = content
        self.catalog_page.set_installing(self._catalog_installing, True)
        self.start_update(entries, folder=folder)
        if not self.update_worker:
            self.catalog_page.set_installing(self._catalog_installing, False)
            self._catalog_installing = []

    def _installed_version(self, project_id: str) -> Optional[str]:
        """Установленная версия проекта (для отметки в списке версий каталога)."""
        content = self.catalog_page.content
        data = self.mods_data if content is self.content else self.sessions.get(self._skey(content=content)) or []
        return next((e.get("current_version") for e in iter_all_entries(data)
                     if e.get("project_id") == project_id and e.get("filepath")), None)

    def _show_installed_from_catalog(self, hit: dict):
        content = self.catalog_page.content
        self._switch_page(self.PAGE_MODS_INDEX)
        if self.content is not content:
            self._set_content(content)
        entry = next((e for e in iter_all_entries(self.mods_data) if e.get("project_id") == hit.get("project_id")
                      and e.get("filepath")), None)
        if entry:
            self._reset_filters()
            self._select_entry(entry)
            self._open_current_details()
        else:
            self.toasts.show("Мод установлен — список обновится после чтения папки.", "info")

    # ================================================================== Режим «Минимум»

    def _apply_minimal(self):
        minimal = bool(self.settings.get("minimal_mode", False))
        self._minimal = minimal
        for widget in (self.tabs_widget, self.chips_widget):
            widget.setVisible(not minimal)
        for widget in (self.select_all, self.tag_combo, self.sort_combo, self.edit_btn):
            widget.setVisible(not minimal)
        self.update_all_btn.setMinimumHeight(48 if minimal else 36)
        self.update_all_btn.setProperty("big", "true" if minimal else "false")
        self.update_all_btn.style().unpolish(self.update_all_btn)
        self.update_all_btn.style().polish(self.update_all_btn)
        self._refresh_empty_state()

    # ================================================================== Горячие клавиши

    def _action_callbacks(self) -> Dict[str, Callable]:
        return {
            "check": self._on_check_clicked, "update_all": self.update_all, "palette": self.palette.open,
            "search": self._focus_search, "undo": self._undo_last,
            "favorite": lambda: self._current_entry() and self._toggle_favorite([self._current_entry()]),
            "pin": lambda: self._current_entry() and self._current_entry().get("filepath") and self._toggle_pin(self._current_entry()),
            "disable": lambda: self._current_entry() and self._toggle_disabled([self._current_entry()]),
            "note": self._edit_note_current, "mods": lambda: self._switch_page(self.PAGE_MODS_INDEX),
            "catalog": lambda: self._switch_page(self.PAGE_CATALOG_INDEX),
            "history": lambda: self._switch_page(self.PAGE_HISTORY_INDEX),
            "stats": lambda: self._switch_page(self.PAGE_STATS_INDEX),
            "snapshot": lambda: self._create_snapshot("manual"), "crash": self._show_crash_dialog,
        }

    def _setup_action_shortcuts(self):
        for shortcut in getattr(self, "_shortcuts", {}).values():
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        self._shortcuts = {}
        callbacks = self._action_callbacks()
        for key, sequence in effective_shortcuts(self.settings.get("shortcuts")).items():
            if sequence and key in callbacks:
                self._shortcuts[key] = QShortcut(QKeySequence(sequence), self, activated=callbacks[key])
        self.check_btn.setToolTip(f"Проверить обновления ({effective_shortcuts(self.settings.get('shortcuts'))['check']})")

    # ================================================================== Подсказки в строке поиска

    def _setup_query_completer(self):
        self._query_model = QStringListModel(self)
        self._completer = QCompleter(self._query_model, self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.setWidget(self.search)
        self._completer.activated[str].connect(self._insert_completion)
        self.search.textEdited.connect(self._on_search_edited)
        self.search.setToolTip("Можно искать с условиями: tag:оптимизация, side:client, is:fav, is:pinned, "
                               "status:update, size>5mb, age>1y. Минус в начале — «без»: -tag:библиотека")

    def _on_search_edited(self, text: str):
        token = text.split(" ")[-1] if text else ""
        if len(token) < 2:
            self._completer.popup().hide()
            return
        hints = [h for h, _ in QUERY_HINTS]
        hints += [f'tag:"{title}"' if " " in title else f"tag:{title}" for _, title, _ in collect_tags(self._count_items, self.mod_tags)]
        matches = [h for h in hints if h.lower().startswith(token.lower()) and h.lower() != token.lower()]
        if not matches:
            self._completer.popup().hide()
            return
        self._query_model.setStringList(matches[:12])
        self._completer.setCompletionPrefix(token)
        self._completer.complete()

    def _insert_completion(self, completion: str):
        text = self.search.text()
        head = text[:len(text) - len(text.split(" ")[-1])]
        self.search.setText(head + completion + ("" if completion.endswith(":") else " "))

    # ================================================================== Перезапуск (масштаб интерфейса)

    def _restart_app(self):
        if self._busy:
            self.toasts.show("Дождитесь окончания текущей операции.", "warning")
            return
        args = sys.argv[1:] if getattr(sys, "frozen", False) else [os.path.abspath(sys.argv[0])] + sys.argv[1:]
        self.save_config()
        if QProcess.startDetached(sys.executable, args):
            QApplication.instance().closeAllWindows()

    # ================================================================== Установка и внешние ссылки

    def _refresh_install_ui(self):
        import installer
        supported = installer.supported()
        self.settings_page.set_install_state(supported, installer.state() if supported else None)

    def _offer_install(self):
        """Один раз предлагаем установить программу, если она запущена просто как .exe."""
        import installer
        if not installer.supported() or self.settings.get("install_offered") or installer.state().installed:
            return
        self.settings["install_offered"] = True
        self.save_config()
        self.toasts.show("Установите программу: ярлыки, открытие .mrpack двойным щелчком и ссылки modupdater://.",
                         "info", "Подробнее", lambda: self._switch_page(self.PAGE_SETTINGS_INDEX), timeout=12000)

    def _install_app(self, options: dict):
        import installer
        try:
            done = installer.install(sys.executable, **options)
        except Exception as e:
            show_message(self, QMessageBox.Icon.Critical, "Установка не удалась", str(e))
            return
        self._refresh_install_ui()
        state = installer.state()
        text = "Готово:\n" + "\n".join(f"• {step}" for step in done)
        if not state.running_installed:
            if confirm(self, "Программа установлена", text + "\n\nЗапустить установленную копию и закрыть эту?",
                       "Запустить", "Позже"):
                self.save_config()
                if QProcess.startDetached(state.exe, []):
                    QApplication.instance().closeAllWindows()
        else:
            show_message(self, QMessageBox.Icon.Information, "Настройки установки применены", text)

    def _uninstall_app(self):
        import installer
        if not confirm(self, "Удалить программу", "Удалить ярлыки, связь с .mrpack и ссылками и саму программу? "
                       "Сборки, моды и бэкапы не затрагиваются, настройки сохранятся.", "Удалить", "Отмена"):
            return
        installer.uninstall(schedule_exe_removal=installer.state().running_installed)
        show_message(self, QMessageBox.Icon.Information, "Программа удалена",
                     "Ярлыки и записи удалены. Окно закроется — папка программы будет удалена следом.")
        QApplication.instance().closeAllWindows()

    def handle_external(self, args: List[str]):
        """Файл .mrpack или ссылка modupdater://, переданные при запуске или второй копией программы."""
        import urllib.parse
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()
        QApplication.alert(self)
        for arg in args or []:
            if arg == "--activate":
                continue
            if arg.lower().endswith(".mrpack") and os.path.isfile(arg):
                self._switch_page(self.PAGE_MODS_INDEX)
                self._import_mrpack(arg)
                continue
            if not arg.lower().startswith("modupdater:"):
                continue
            parsed = urllib.parse.urlparse(arg)
            action = (parsed.netloc or parsed.path.strip("/").split("/")[0]).lower()
            rest = parsed.path.strip("/") if parsed.netloc else "/".join(parsed.path.strip("/").split("/")[1:])
            value = urllib.parse.unquote(rest)
            query = urllib.parse.parse_qs(parsed.query)
            if action == "import" and value:
                self._switch_page(self.PAGE_MODS_INDEX)
                self._import_text(value)
            elif action == "mod" and value:  # modupdater://mod/sodium — найти в каталоге
                self._switch_page(self.PAGE_CATALOG_INDEX)
                self.catalog_page.search.setText(value)
            elif action == "profile" and value in self.profiles:
                self._switch_page(self.PAGE_MODS_INDEX)
                self._on_profile_selected(value)
                self._refresh_sidebar()
            elif action == "mrpack" and query.get("url"):
                self._open_remote_mrpack(query["url"][0])
            else:
                self.toasts.show(f"Неизвестная ссылка: {arg}", "warning")

    def _open_remote_mrpack(self, url: str):
        if not url.lower().startswith("https://"):
            self.toasts.show("Сборку можно открыть только по адресу https://", "warning")
            return
        import tempfile
        from mod_updater_logic import download_file
        target = os.path.join(tempfile.gettempdir(), "modupdater-import.mrpack")
        self._run_task(download_file, (url, target), lambda _: self._import_mrpack(target),
                       lambda e: self.toasts.show(f"Не удалось скачать сборку: {e}", "error"), "Скачиваем сборку…")

    # ================================================================== Мелочи для меню

    def _abandoned(self, entry: dict) -> bool:
        return bool(self.settings.get("abandoned_warning", True)) and bool(entry.get("filepath")) and is_abandoned(entry)

    def _preset_key(self, entry: dict) -> str:
        return preset_key(entry)
