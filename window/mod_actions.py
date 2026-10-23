"""Действия с модами: отмена (Ctrl+Z), избранное, заметки, массовые действия, наборы.

Примесь к главному окну (ModUpdaterApp): методы работают с его атрибутами."""
import copy
from typing import Callable, Dict, List, Optional

from PyQt6.QtWidgets import QMenu, QMessageBox

from content import MODS
from enums import ModStatus
from i18n import tr
from maintenance import enabled_keys, preset_plan
from ui.dialogs import ask_text
from ui.widgets import make_action
from ui_components import confirm, show_message
from updater import set_disabled
from view_model import iter_all_entries, pin_key
from window.common import UNDO_LIMIT


class ModActionsMixin:
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
