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
import time
import sys
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import QStringListModel, Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import QApplication, QCompleter, QFileDialog, QMenu, QMessageBox

import config as app_config
import single_instance
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
from i18n import tr

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
            self.toasts.show(tr("Дождитесь окончания текущей операции."), "warning")
            return
        if not self._undo:
            self.toasts.show(tr("Отменять нечего."), "info")
            return
        description, action = self._undo.pop()
        self._undoing = True
        try:
            action()
        finally:
            self._undoing = False
        self.toasts.show(tr('Отменено: {0}', description), "success")

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
        self._push_undo(tr("избранное"), lambda: self._restore_favorites(before))
        self._after_personal_change(entries)
        what = f"«{entries[0].get('name')}»" if len(entries) == 1 else tr('{0} модов', len(entries))
        self.toasts.show(f"{what} {tr('в избранном') if make_favorite else tr('убран из избранного')}", "success")

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
                                       tr("Открепить") if all_pinned else tr("Закрепить версии"),
                                       lambda: self._set_pins(files, not all_pinned)))
        menu.addAction(make_action(menu, t, "star", tr("Убрать из избранного") if all_fav else tr("В избранное"),
                                   lambda: self._toggle_favorite(checked)))
        menu.addAction(make_action(menu, t, "tag", tr("Добавить метку…"), lambda: self._add_tag_many(checked)))
        if files:
            menu.addAction(make_action(menu, t, "camera", tr("Сохранить как набор модов…"),
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
        self._push_undo(tr("закрепление"), lambda: self._restore_pins(before))
        self._set_data(self.mods_data)
        self.save_config()
        self.toasts.show(f"{tr('Закреплено') if pinned else tr('Откреплено')}: {len(entries)}", "success")

    def _restore_pins(self, pins: Dict[str, str]):
        profile_pins = self._profile().setdefault("pinned", {})
        profile_pins.clear()
        profile_pins.update(pins)
        self._set_data(self.mods_data)
        self.save_config()

    def _add_tag_many(self, entries: List[dict]):
        tag = ask_text(self, tr("Новая метка"), tr('Метка для {0} модов', len(entries)), tr("Название метки"))
        if not tag:
            return
        before = copy.deepcopy(self.mod_tags)
        for entry in entries:
            tags = self.mod_tags.setdefault(pin_key(entry), [])
            if tag.lower() not in (t.lower() for t in tags):
                tags.append(tag)
        self._push_undo(tr("метки"), lambda: self._restore_tags(before))
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
        hint = tr("Пусто — без группы") + (tr('\nУже есть: {0}', ', '.join(groups)) if groups else "")
        group = ask_text(self, tr("Группа сборки"), hint, tr("Например: Серверы"), self.profiles[name].get("group") or "")
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
            self.toasts.show(tr('Сборка «{0}» создана: скопировано {1}.', new_name, ', '.join(copied)), "success",
                             tr("Сравнить"), lambda: self._compare_profiles(name, new_name), timeout=9000)

        self._run_task(clone_game_dir, (profile["mods_path"], target, with_saves), done,
                       lambda e: show_message(self, QMessageBox.Icon.Critical, tr("Не удалось клонировать"), e),
                       tr("Копируем сборку…"))

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
                       lambda e: show_message(self, QMessageBox.Icon.Warning, tr("Сравнение"), e), tr("Сравниваем сборки…"))

    # ================================================================== Наборы модов

    def _presets(self) -> Dict[str, List[str]]:
        return self._profile().setdefault("presets", {})

    def _refresh_presets(self):
        presets = self._presets()
        current = enabled_keys(self._count_items) if self.content is MODS else []
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        active = next((name for name, keys in presets.items() if sorted(keys) == current), None)
        self.preset_combo.addItem(tr("Набор: свой") if not active else tr('Набор: {0}', active), None)
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
        menu.addAction(make_action(menu, t, "camera", tr("Сохранить включённые моды как набор…"),
                                   lambda: self._save_preset([e for e in self._count_items if e.get("filepath")])))
        presets = self._presets()
        if presets:
            menu.addSeparator()
            for name in presets:
                sub = menu.addMenu(name)
                sub.addAction(make_action(sub, t, "check", tr("Применить"), lambda _=False, n=name: self._apply_preset(n)))
                sub.addAction(make_action(sub, t, "refresh", tr("Перезаписать текущими"),
                                          lambda _=False, n=name: self._overwrite_preset(n)))
                sub.addAction(make_action(sub, t, "trash", tr("Удалить"), lambda _=False, n=name: self._delete_preset(n), "danger"))
        menu.exec(self.preset_btn.mapToGlobal(self.preset_btn.rect().bottomLeft()))

    def _save_preset(self, entries: List[dict]):
        name = ask_text(self, tr("Новый набор"), tr("Название набора — например, «Для записи видео» или «Слабый ПК».\n"
                        "В набор попадут моды, которые сейчас включены."), tr("Название"))
        if not name:
            return
        keys = enabled_keys(entries)
        self._presets()[name] = keys
        self._refresh_presets()
        self.save_config()
        self.toasts.show(tr('Набор «{0}» сохранён: {1} модов.', name, len(keys)), "success")

    def _overwrite_preset(self, name: str):
        self._presets()[name] = enabled_keys(self._count_items)
        self._refresh_presets()
        self.save_config()
        self.toasts.show(tr('Набор «{0}» обновлён.', name), "success")

    def _delete_preset(self, name: str):
        if confirm(self, tr("Удалить набор"), tr('Удалить набор «{0}»? Моды не изменятся.', name), tr("Удалить"), tr("Отмена")):
            self._presets().pop(name, None)
            self._refresh_presets()
            self.save_config()

    def _apply_preset(self, name: str):
        keys = self._presets().get(name)
        if keys is None or self._busy:
            return
        plan = preset_plan(self._count_items, keys)
        if not plan["enable"] and not plan["disable"]:
            self.toasts.show(tr('Набор «{0}» уже применён.', name), "info")
            return
        changed = plan["enable"] + plan["disable"]
        if not self._confirm_game_not_running([e["filepath"] for e in changed]):
            return
        before = enabled_keys(self._count_items)
        errors = self._rename_entries(plan["disable"], True) + self._rename_entries(plan["enable"], False)
        self._set_data(self.mods_data)
        self.sessions[self._skey()] = self.mods_data
        self._push_undo(tr('набор «{0}»', name), lambda: self._apply_keys(before))
        self.toasts.show(tr('Набор «{0}»: включено {1}, выключено {2}.', name, len(plan['enable']), len(plan['disable'])), "success",
                         tr("Отменить"), self._undo_last, timeout=8000)
        if errors:
            show_message(self, QMessageBox.Icon.Warning, tr("Не всё переключилось"), "\n".join(errors[:10]))

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
            self.toasts.show(tr("Для поиска нужно хотя бы два включённых мода."), "warning")
            return
        steps = max(1, math.ceil(math.log2(len(enabled))))
        if not confirm(self, tr("Поиск проблемного мода"),
                       tr('Программа будет по очереди выключать половину модов, а вы — запускать игру и отвечать, осталась ли проблема. Понадобится около {0} запусков игры.\n\nМоды, которые нужны выключенным, выключаются вместе с ними — ложных ошибок из-за зависимостей не будет. В конце всё вернётся как было. Начать?', steps), tr("Начать"), tr("Отмена")):
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
        auto = False
        if self._auto_bisect_available():
            plan = self._launch_plan()
            choice = ask_choice(self, QMessageBox.Icon.Question, tr("Как проверять запуск?"),
                                tr('<b>Автоматически</b> — программа сама запускает игру через {0}, ждёт главного меню или вылета, закрывает игру и делает следующий шаг. Подходит, если игра <b>вылетает при запуске</b>.<br><br><b>Вручную</b> — игру запускаете вы и отвечаете кнопками. Подходит для любой проблемы, в том числе в мире.', plan.title),
                                [tr("Автоматически"), tr("Вручную"), tr("Отмена")], default=0, rich_text=True)
            if choice not in (0, 1):
                return
            auto = choice == 0
        self._profile()["bisect"] = session.to_dict()
        self.save_config()
        self._apply_bisect_state(session)
        if auto:
            self._start_auto_step()

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
            show_message(self, QMessageBox.Icon.Warning, tr("Не всё переключилось"),
                         tr("Закройте игру и повторите.\n\n") + "\n".join(errors[:10]))

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
        self._auto_bisect = None
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
        self.toasts.show(tr("Поиск завершён: ") + (tr('«{0}» выключен, остальное вернулось как было.', session.culprit)
                                               if keep_culprit_disabled else tr("все моды вернулись как были.")), "success")

    def _cancel_bisect(self):
        if confirm(self, tr("Прервать поиск"), tr("Вернуть все моды в состояние до начала поиска?"), tr("Вернуть"), tr("Продолжить поиск")):
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

    def _show_crash_dialog(self, preferred: Optional[str] = None):
        game_dir = self._profile().get("mods_path", "")
        if not game_dir:
            return
        dialog = CrashDialog(self, self.tokens, find_crash_files(game_dir), self._installed_mods(), self._run_task,
                             preferred if isinstance(preferred, str) else None)
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
                self.toasts.show(tr("Дождитесь окончания текущей операции."), "warning")
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
                self.toasts.show(tr('Нет версии для вашей сборки: {0}', ', '.join(titles)), "warning")
            if entries:
                self._start_catalog_install(entries, content)

        self._run_task(resolve_many_installs, (ids, self.game_version, self.loader, self._channel(), installed, content),
                       resolved, lambda e: (self.catalog_page.set_installing(ids, False),
                                            self.toasts.show(tr('Не удалось подготовить установку: {0}', e), "error")), None)

    def _install_catalog_version(self, hit: dict, version: dict):
        if self._busy:
            self.toasts.show(tr("Дождитесь окончания текущей операции."), "warning")
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
            self.toasts.show(tr("Мод установлен — список обновится после чтения папки."), "info")

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
        self.check_btn.setToolTip(tr('Проверить обновления ({0})', effective_shortcuts(self.settings.get('shortcuts'))['check']))

    # ================================================================== Подсказки в строке поиска

    def _setup_query_completer(self):
        self._query_model = QStringListModel(self)
        self._completer = QCompleter(self._query_model, self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.setWidget(self.search)
        self._completer.activated[str].connect(self._insert_completion)
        self.search.textEdited.connect(self._on_search_edited)
        self.search.setToolTip(tr("Можно искать с условиями: tag:оптимизация, side:client, is:fav, is:pinned, "
                               "status:update, size>5mb, age>1y. Минус в начале — «без»: -tag:библиотека"))

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
            self.toasts.show(tr("Дождитесь окончания текущей операции."), "warning")
            return
        self.save_config()
        try:
            single_instance.launch_new_instance(single_instance.external_args(sys.argv))
        except OSError as e:
            show_message(self, QMessageBox.Icon.Critical, tr("Не удалось перезапустить"), str(e))
            return
        QApplication.instance().closeAllWindows()

    # ================================================================== Обновление программы

    def _refresh_app_update_ui(self, checking: bool = False):
        release = getattr(self, "_app_release", None)
        current = app_config.APP_VERSION
        if checking:
            text = tr("Версия {0} · проверяем…", current)
        elif release:
            text = tr("Версия {0} · <span style='color:{1}'><b>доступна {2}</b></span>", current, self.tokens["accent"],
                      release.version)
        elif self.settings.get("app_update_last_check"):
            text = tr("Версия {0} · это последняя версия", current)
        else:
            text = tr("Версия {0}", current)
        self.settings_page.set_app_update_state(text, available=bool(release) and not checking, checking=checking)

    def _check_app_update(self, manual: bool = False):
        """Новая версия программы на GitHub. Автоматически — при запуске, не чаще раза в 12 часов."""
        import self_update
        if not manual:
            known = self_update.parse_version(self.settings.get("app_update_latest") or "")
            current = self_update.parse_version(app_config.APP_VERSION)
            newer_known = bool(known and current and known > current)  # Уже находили — напомним без ожидания
            if not self.settings.get("app_update_check", True) or \
                    not (newer_known or self_update.needs_check(self.settings.get("app_update_last_check"))):
                return
        self._refresh_app_update_ui(checking=True)
        self._run_task(self_update.check, (), lambda release: self._on_app_update(release, manual),
                       lambda error: self._on_app_update_failed(error, manual), None)

    def _on_app_update(self, release, manual: bool):
        self.settings["app_update_last_check"] = time.time()
        self.settings["app_update_latest"] = release.version if release else ""
        self.save_config()
        self._app_release = release
        self._refresh_app_update_ui()
        if release is None:
            if manual:
                self.toasts.show(tr("У вас последняя версия программы."), "success")
            return
        if manual:
            self._open_app_update()
        elif self.settings.get("app_update_skip") != release.version:
            self.toasts.show(tr("Доступна новая версия программы: {0}", release.version), "info", tr("Подробнее"),
                             self._open_app_update, timeout=15000)

    def _on_app_update_failed(self, error: str, manual: bool):
        self._refresh_app_update_ui()
        if manual:
            self.toasts.show(tr("Не удалось проверить обновления программы: {0}", error), "warning")

    def _open_app_update(self):
        import self_update
        from ui.dialogs import AppUpdateDialog
        release = getattr(self, "_app_release", None)
        if release is None:
            return
        if self._busy:
            self.toasts.show(tr("Дождитесь окончания текущей операции."), "warning")
            return
        dialog = AppUpdateDialog(self, release, app_config.APP_VERSION, self_update.supported())
        accepted = dialog.exec()
        if dialog.skipped:
            self.settings["app_update_skip"] = release.version
            self.save_config()
            self.toasts.show(tr("Версия {0} пропущена — напомним о следующей.", release.version), "info")
        elif accepted and dialog.downloaded_path:
            self._install_app_update(dialog.downloaded_path)

    def _install_app_update(self, path: str):
        """Ставит скачанный .exe вместо текущего и перезапускает программу."""
        import self_update
        if self._busy:
            self.toasts.show(tr("Дождитесь окончания текущей операции и обновите программу снова."), "warning")
            return
        try:
            self_update.replace_executable(path, self_update.current_executable())
        except OSError as e:
            show_message(self, QMessageBox.Icon.Critical, tr("Не удалось обновить программу"), str(e))
            return
        self._restart_app()

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
        self.toasts.show(tr("Установите программу: ярлыки, открытие .mrpack двойным щелчком и ссылки modupdater://."),
                         "info", tr("Подробнее"), lambda: self._switch_page(self.PAGE_SETTINGS_INDEX), timeout=12000)

    def _install_app(self, options: dict):
        import installer
        try:
            done = installer.install(sys.executable, **options)
        except Exception as e:
            show_message(self, QMessageBox.Icon.Critical, tr("Установка не удалась"), str(e))
            return
        self._refresh_install_ui()
        state = installer.state()
        text = tr("Готово:\n") + "\n".join(f"• {step}" for step in done)
        if not state.running_installed:
            if confirm(self, tr("Программа установлена"), text + tr("\n\nЗапустить установленную копию и закрыть эту?"),
                       tr("Запустить"), tr("Позже")):
                self.save_config()
                try:
                    single_instance.launch_new_instance(executable=state.exe)
                except OSError as e:
                    show_message(self, QMessageBox.Icon.Critical, tr("Не удалось запустить"), str(e))
                    return
                QApplication.instance().closeAllWindows()
        else:
            show_message(self, QMessageBox.Icon.Information, tr("Настройки установки применены"), text)

    def _uninstall_app(self):
        import installer
        if not confirm(self, tr("Удалить программу"), tr("Удалить ярлыки, связь с .mrpack и ссылками и саму программу? "
                       "Сборки, моды и бэкапы не затрагиваются, настройки сохранятся."), tr("Удалить"), tr("Отмена")):
            return
        installer.uninstall(schedule_exe_removal=installer.state().running_installed)
        show_message(self, QMessageBox.Icon.Information, tr("Программа удалена"),
                     tr("Ярлыки и записи удалены. Окно закроется — папка программы будет удалена следом."))
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
                self.toasts.show(tr('Неизвестная ссылка: {0}', arg), "warning")

    def _open_remote_mrpack(self, url: str):
        if not url.lower().startswith("https://"):
            self.toasts.show(tr("Сборку можно открыть только по адресу https://"), "warning")
            return
        import tempfile
        from mod_updater_logic import download_file
        target = os.path.join(tempfile.gettempdir(), "modupdater-import.mrpack")
        self._run_task(download_file, (url, target), lambda _: self._import_mrpack(target),
                       lambda e: self.toasts.show(tr('Не удалось скачать сборку: {0}', e), "error"), tr("Скачиваем сборку…"))

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

    # ================================================================== Мелочи для меню

    def _abandoned(self, entry: dict) -> bool:
        return bool(self.settings.get("abandoned_warning", True)) and bool(entry.get("filepath")) and is_abandoned(entry)

    def _preset_key(self, entry: dict) -> str:
        return preset_key(entry)

    # ================================================================== Игра: запуск, наблюдение, вылеты

    def _game_dir(self) -> str:
        return self._profile().get("mods_path", "")

    def _launch_plan(self):
        import game_runner
        return game_runner.launch_plan(self._profile())

    def _refresh_play_button(self):
        plan = self._launch_plan()
        running = self._game_running
        self.play_btn.setVisible(bool(self._game_dir()))
        if running:
            self.play_btn.setText(tr("Игра запущена"))
            self.play_btn.setEnabled(False)
        else:
            self.play_btn.setText(tr("Играть"))
            self.play_btn.setEnabled(bool(plan) and not self._busy and not self._auto_bisect)
        tip = (tr('Запустить сборку ({0})', plan.title) if plan and plan.opens_instance else
               tr('Открыть {0} (выберите сборку там)', plan.title) if plan else
               tr("Не удалось найти лаунчер — укажите команду запуска в настройках сборки"))
        self.play_btn.setToolTip(tip)

    def _play(self):
        import game_runner
        plan = self._launch_plan()
        if not plan:
            self.toasts.show(tr("Не удалось найти лаунчер для этой сборки."), "warning", tr("Настроить"), self._edit_current_profile)
            return
        try:
            game_runner.launch(plan)
        except OSError as e:
            show_message(self, QMessageBox.Icon.Critical, tr("Не удалось запустить"), str(e))
            return
        self._launched_at = time.time()
        self.toasts.show(tr('Запускаем игру через {0}…', plan.title) if plan.opens_instance else
                         tr('Открыт {0} — запустите сборку «{1}» там.', plan.title, self.current_profile_name), "info")

    def _poll_game(self):
        """Раз в несколько секунд: запущена ли игра этой сборки (файлы модов заняты) и не вылетела ли она."""
        import game_runner
        from updater import is_game_running
        game_dir = self._game_dir()
        mods = MODS.path(game_dir)
        if not game_dir or not os.path.isdir(mods) or self._busy == "update":
            return
        running = is_game_running(mods)
        if self._auto_bisect:
            self._auto_bisect_poll(running)
            return
        if running and not self._game_running:
            self._game_started_at = self._launched_at or time.time()
            self._set_game_running(True)
        elif not running and self._game_running:
            self._set_game_running(False)
            self._after_game_exit(self._game_started_at)
        elif not running and self._launched_at and time.time() - self._launched_at < 300:
            # Игра могла упасть ещё до того, как заняла файлы модов (ошибка загрузчика)
            state = game_runner.run_state(game_dir, self._launched_at)
            if state.crashed:
                launched, self._launched_at = self._launched_at, None
                self._after_game_exit(launched, state)

    def _set_game_running(self, running: bool):
        self._game_running = running
        self.game_banner.setVisible(running)
        self._refresh_play_button()
        self._refresh_actions()

    def _after_game_exit(self, started_at: Optional[float], state=None):
        import game_runner
        self._launched_at = None
        game_dir = self._game_dir()
        state = state or game_runner.run_state(game_dir, started_at or time.time() - 3600)
        if not state.crashed:
            return
        logging.info(tr('Игра завершилась с ошибкой: {0}', state.reason))
        QApplication.alert(self)
        if self.settings.get("auto_crash_analysis", True):
            self.toasts.show(tr("Игра вылетела — открываем разбор."), "warning")
            self._show_crash_dialog(preferred=state.crash_file)
        else:
            self.toasts.show(tr("Игра вылетела."), "warning", tr("Разобрать"), lambda: self._show_crash_dialog(state.crash_file),
                             timeout=12000)

    def _stop_game(self):
        import game_runner
        if confirm(self, tr("Закрыть игру"), tr("Закрыть Minecraft этой сборки? Несохранённый прогресс в мире может потеряться."),
                   tr("Закрыть"), tr("Отмена")):
            self._run_task(game_runner.stop_game, (self._game_dir(),),
                           lambda n: self.toasts.show(tr("Игра закрыта.") if n else tr("Процесс игры не найден."), "info"),
                           lambda e: self.toasts.show(tr('Не удалось закрыть игру: {0}', e), "error"), None)

    # ================================================================== Автоматический поиск проблемного мода

    def _auto_bisect_available(self) -> bool:
        plan = self._launch_plan()
        return bool(plan and plan.opens_instance)

    def _start_auto_step(self):
        """Шаг автопоиска: состояние модов уже применено — запускаем игру и ждём меню или вылета."""
        import game_runner
        session = self._bisect_session()
        if not session or session.done:
            self._auto_bisect = None
            return
        plan = self._launch_plan()
        self._auto_bisect = {"phase": "starting", "since": time.time(), "seen_running": False}
        try:
            game_runner.launch(plan)
        except OSError as e:
            self._auto_bisect = None
            show_message(self, QMessageBox.Icon.Critical, tr("Не удалось запустить игру"), str(e))
            self._restore_bisect_banner()
            return
        self._show_auto_status(tr("запускаем игру и ждём главного меню…"))

    def _show_auto_status(self, text: str):
        session = self._bisect_session()
        if session:
            self.bisect_banner.show_auto(session.step + 1, session.step + session.steps_left(),
                                         len(session.disabled_now), len(session.suspects), text)

    def _auto_bisect_poll(self, running: bool):
        import game_runner
        auto = self._auto_bisect
        if not auto or auto.get("phase") != "starting":
            return
        auto["seen_running"] = auto["seen_running"] or running
        state = game_runner.run_state(self._game_dir(), auto["since"])
        timeout = int(self.settings.get("auto_bisect_timeout", 5)) * 60
        if state.crashed:
            self._auto_answer(True, tr('вылет ({0})', state.reason))
        elif state.reached_menu:
            self._auto_answer(False, tr("игра дошла до меню"))
        elif auto["seen_running"] and not running:
            self._auto_answer(True, tr("игра закрылась, не дойдя до меню"))
        elif time.time() - auto["since"] > timeout:
            self._auto_bisect = None
            self.toasts.show(tr("Игра не загрузилась за отведённое время — ответьте на этот шаг вручную."), "warning")
            self._restore_bisect_banner()
        elif auto["seen_running"]:
            self._show_auto_status(tr("игра загружается…"))

    def _auto_answer(self, problem_persists: bool, reason: str):
        import game_runner
        self._auto_bisect["phase"] = "stopping"
        logging.info(tr('Автопоиск: {0} → проблема {1}', reason, tr('осталась') if problem_persists else tr('пропала')))
        self._show_auto_status(tr('{0}; закрываем игру…', reason))
        game_dir = self._game_dir()

        def wait_closed():
            from updater import is_game_running
            game_runner.stop_game(game_dir)
            deadline = time.time() + 30
            while is_game_running(MODS.path(game_dir)) and time.time() < deadline:
                time.sleep(1)

        def next_step(_=None):
            if not self._auto_bisect:  # Остановлено пользователем
                return
            self._on_bisect_answer(problem_persists)
            session = self._bisect_session()
            if session and not session.done:
                self._start_auto_step()
            else:
                self._auto_bisect = None

        self._run_task(wait_closed, (), next_step, lambda e: next_step(), None)

    def _stop_auto_bisect(self):
        self._auto_bisect = None
        self.toasts.show(tr("Автоматический поиск остановлен — продолжайте вручную или прервите поиск."), "info")
        self._restore_bisect_banner()
