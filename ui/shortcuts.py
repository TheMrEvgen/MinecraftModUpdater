"""Горячие клавиши: список действий, сочетания по умолчанию и редактор с проверкой конфликтов."""
from typing import Dict, List, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import QGridLayout, QKeySequenceEdit, QLabel, QPushButton, QWidget

# (действие, подпись, сочетание по умолчанию)
ACTIONS = (
    ("check", "Проверить обновления", "F5"),
    ("update_all", "Обновить все", "Ctrl+U"),
    ("palette", "Быстрые команды", "Ctrl+K"),
    ("search", "Поиск", "Ctrl+F"),
    ("undo", "Отменить последнее действие", "Ctrl+Z"),
    ("favorite", "Избранное для выбранного мода", "Ctrl+D"),
    ("pin", "Закрепить / открепить выбранный мод", "Ctrl+L"),
    ("disable", "Выключить / включить выбранный мод", "Ctrl+E"),
    ("note", "Заметка к выбранному моду", "Ctrl+N"),
    ("catalog", "Открыть каталог", "Ctrl+2"),
    ("mods", "Открыть список модов", "Ctrl+1"),
    ("history", "Открыть историю", "Ctrl+3"),
    ("stats", "Открыть статистику", "Ctrl+4"),
    ("snapshot", "Создать снимок сборки", "Ctrl+Shift+S"),
    ("crash", "Разобрать краш игры", "Ctrl+Shift+C"),
)
DEFAULTS = {key: default for key, _, default in ACTIONS}
# Клавиши, занятые списком (их нельзя назначить действиям)
RESERVED = {"Space", "Return", "Enter", "Delete", "Esc", "Left", "Right", "Up", "Down"}


def effective_shortcuts(overrides: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Сочетания с учётом переназначений пользователя (пустая строка — без сочетания)."""
    result = dict(DEFAULTS)
    for key, value in (overrides or {}).items():
        if key in result:
            result[key] = value
    return result


def find_conflicts(shortcuts: Dict[str, str]) -> Dict[str, List[str]]:
    """Сочетание -> действия, которым оно назначено, если их больше одного."""
    by_sequence: Dict[str, List[str]] = {}
    for key, sequence in shortcuts.items():
        normalized = QKeySequence(sequence).toString() if sequence else ""
        if normalized:
            by_sequence.setdefault(normalized, []).append(key)
    return {seq: keys for seq, keys in by_sequence.items() if len(keys) > 1}


class ShortcutEditor(QWidget):
    """Таблица «действие — сочетание» с кнопкой сброса. Сигнал changed(переназначения)."""
    changed = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._overrides: Dict[str, str] = {}
        self._loading = False
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(6)
        self.edits: Dict[str, QKeySequenceEdit] = {}
        self.warnings: Dict[str, QLabel] = {}
        titles = dict((key, title) for key, title, _ in ACTIONS)
        for row, (key, title, _) in enumerate(ACTIONS):
            grid.addWidget(QLabel(title), row, 0)
            edit = QKeySequenceEdit()
            edit.setMaximumSequenceLength(1)
            edit.setClearButtonEnabled(True)
            edit.setFixedWidth(170)
            edit.keySequenceChanged.connect(lambda _seq, k=key: self._on_changed(k))
            grid.addWidget(edit, row, 1)
            reset = QPushButton("По умолчанию")
            reset.setObjectName("GhostButton")
            reset.clicked.connect(lambda _=False, k=key: self._reset(k))
            grid.addWidget(reset, row, 2)
            warning = QLabel()
            warning.setObjectName("WarningText")
            grid.addWidget(warning, row, 3)
            self.edits[key], self.warnings[key] = edit, warning
        grid.setColumnStretch(3, 1)
        self._titles = titles

    def load(self, overrides: Dict[str, str]):
        self._loading = True
        self._overrides = dict(overrides or {})
        for key, sequence in effective_shortcuts(self._overrides).items():
            self.edits[key].setKeySequence(QKeySequence(sequence))
        self._loading = False
        self._show_conflicts()

    def _reset(self, key: str):
        self.edits[key].setKeySequence(QKeySequence(DEFAULTS[key]))

    def _on_changed(self, key: str):
        if self._loading:
            return
        sequence = self.edits[key].keySequence().toString()
        if sequence in RESERVED:
            self.edits[key].setKeySequence(QKeySequence(DEFAULTS[key]))
            self.warnings[key].setText(f"«{sequence}» занята списком модов")
            return
        if sequence == QKeySequence(DEFAULTS[key]).toString():
            self._overrides.pop(key, None)
        else:
            self._overrides[key] = sequence
        self._show_conflicts()
        self.changed.emit(dict(self._overrides))

    def _show_conflicts(self):
        conflicts = find_conflicts({k: e.keySequence().toString() for k, e in self.edits.items()})
        conflicted = {k: seq for seq, keys in conflicts.items() for k in keys}
        for key, warning in self.warnings.items():
            if key in conflicted:
                others = [self._titles[k] for k in conflicts[conflicted[key]] if k != key]
                warning.setText(f"⚠ то же, что «{others[0]}»")
            else:
                warning.setText("")
