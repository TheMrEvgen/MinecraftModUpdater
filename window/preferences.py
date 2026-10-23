"""Настройки интерфейса: режим «Минимум», горячие клавиши, подсказки поиска.

Примесь к главному окну (ModUpdaterApp): методы работают с его атрибутами."""
from typing import Callable, Dict

from PyQt6.QtCore import QStringListModel, Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import QCompleter

from i18n import tr
from maintenance import preset_key
from query import QUERY_HINTS, is_abandoned
from ui.shortcuts import effective_shortcuts
from view_model import collect_tags


class PreferencesMixin:
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

    # ================================================================== Мелочи для меню

    def _abandoned(self, entry: dict) -> bool:
        return bool(self.settings.get("abandoned_warning", True)) and bool(entry.get("filepath")) and is_abandoned(entry)

    def _preset_key(self, entry: dict) -> str:
        return preset_key(entry)
