"""Инструменты: разбор краша, поиск проблемного мода, сравнение сборок, проверка целостности, клонирование."""
import os
from typing import Callable, Dict, List

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

import icons
from ui.dialogs import FramelessDialog
from ui.widgets import button, label


def _clear(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            _clear(item.layout())


# --- Разбор краша ---

class CrashDialog(FramelessDialog):
    """Отчёт о падении: главное из ошибки и моды, которые в ней упоминаются."""
    disable_requested = pyqtSignal(object)
    show_requested = pyqtSignal(object)
    bisect_requested = pyqtSignal()

    def __init__(self, parent, tokens: dict, files: List[str], entries: List[Dict], run_task: Callable,
                 preferred: str = None):
        super().__init__(parent, "Почему упала игра", 680)
        self.resize(760, 620)
        self.tokens = tokens
        self.entries = entries
        self.run_task = run_task
        self.body.addWidget(label("Программа читает последний краш-репорт или лог игры и ищет моды, которые "
                                  "упоминаются в ошибке. Это подсказка, а не приговор: виновника надёжнее всего "
                                  "находит поиск перебором.", "Muted", wrap=True))
        row = QHBoxLayout()
        row.addWidget(QLabel("Файл:"))
        self.file_combo = QComboBox()
        for path in files:
            name = os.path.basename(path)
            self.file_combo.addItem(name if name != "latest.log" else "latest.log — лог последнего запуска", path)
        if preferred:
            index = self.file_combo.findData(preferred)
            if index < 0 and os.path.isfile(preferred):
                self.file_combo.insertItem(0, os.path.basename(preferred), preferred)
                index = 0
            self.file_combo.setCurrentIndex(max(0, index))
        self.file_combo.currentIndexChanged.connect(self._analyze)
        row.addWidget(self.file_combo, 1)
        self.open_btn = button("", "IconButton", "external", tokens, "text_muted", "Открыть файл")
        self.open_btn.clicked.connect(self._open_file)
        row.addWidget(self.open_btn)
        self.body.addLayout(row)

        self.summary = QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setFixedHeight(120)
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        mono.setPointSizeF(9)
        self.summary.setFont(mono)
        self.body.addWidget(self.summary)
        self.body.addWidget(label("Подозреваемые моды", "SectionTitle"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        self.suspects = QVBoxLayout(holder)
        self.suspects.setContentsMargins(0, 0, 0, 0)
        self.suspects.setSpacing(6)
        scroll.setWidget(holder)
        self.body.addWidget(scroll, 1)

        bisect = self.add_button("Найти виновника перебором…", False, lambda: (self.bisect_requested.emit(), self.accept()))
        bisect.setToolTip("Программа будет выключать половину модов, а вы — проверять запуск игры")
        self.add_button("Закрыть", True, self.accept)
        self.finish_layout()
        if files:
            self._analyze()
        else:
            self.summary.setPlainText("Краш-репортов и лога нет: игра с этой сборкой ещё не запускалась.")

    def _open_file(self):
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        path = self.file_combo.currentData()
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _analyze(self, *_):
        from crash_analysis import analyze_file
        path = self.file_combo.currentData()
        if not path:
            return
        self.summary.setPlainText("Разбираем…")
        _clear(self.suspects)
        self.run_task(analyze_file, (path, self.entries), self._show, lambda e: self.summary.setPlainText(f"Ошибка: {e}"),
                      None)

    def _show(self, report):
        t = self.tokens
        self.summary.setPlainText("\n".join(report.summary) or "В файле не найдено описания ошибки.")
        _clear(self.suspects)
        if not report.suspects:
            self.suspects.addWidget(label("Ни один мод не упоминается явно. Попробуйте поиск перебором.", "Muted", wrap=True))
        top = report.suspects[0].score if report.suspects else 1
        for suspect in report.suspects:
            card = QFrame()
            card.setObjectName("Card")
            row = QHBoxLayout(card)
            row.setContentsMargins(12, 8, 8, 8)
            level = "высокая" if suspect.score >= top * 0.6 else "средняя" if suspect.score >= top * 0.3 else "низкая"
            color = t["danger"] if level == "высокая" else t["warning"] if level == "средняя" else t["text_muted"]
            text = label(f"<b>{suspect.entry.get('name')}</b>   <span style='color:{color}'>● вероятность {level}</span>"
                         f"<br><span style='color:{t['text_faint']}'>{'; '.join(suspect.reasons)}</span>", None, wrap=True)
            text.setTextFormat(Qt.TextFormat.RichText)
            row.addWidget(text, 1)
            show = button("", "IconButton", "search", t, "text_muted", "Показать в списке")
            show.clicked.connect(lambda _=False, e=suspect.entry: (self.show_requested.emit(e), self.accept()))
            row.addWidget(show)
            if not suspect.entry.get("disabled"):
                off = button("Выключить", None, "power", t, "text")
                off.clicked.connect(lambda _=False, e=suspect.entry: self._disable(e))
                row.addWidget(off)
            self.suspects.addWidget(card)
        self.suspects.addStretch()

    def _disable(self, entry: Dict):
        self.disable_requested.emit(entry)
        self._show_after_disable(entry)

    def _show_after_disable(self, entry: Dict):
        self.summary.appendPlainText(f"\n→ «{entry.get('name')}» выключен. Запустите игру и проверьте.")


# --- Поиск проблемного мода: панель шагов над списком ---

class BisectBanner(QFrame):
    """Плашка над списком модов на время поиска: шаг, вопрос и кнопки ответа."""
    answered = pyqtSignal(bool)     # True — проблема осталась
    cancel_requested = pyqtSignal()
    auto_stop_requested = pyqtSignal()
    finish_requested = pyqtSignal(bool)  # True — оставить виновника выключенным

    def __init__(self, tokens: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("Banner")
        self.tokens = tokens
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 10, 10)
        layout.setSpacing(10)
        self.icon = QLabel()
        self.icon.setPixmap(icons.pixmap("scissors", tokens["info"], 20))
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        self.text = label("", None, wrap=True)
        self.text.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.text, 1)
        self.yes = button("Проблема осталась", "DangerButton", "alert", tokens, "danger")
        self.no = button("Всё работает", "PrimaryButton", "check", tokens, "accent_text")
        self.stop = button("Прервать", "GhostButton", "x", tokens, "text_muted", "Вернуть все моды как было")
        self.keep = button("Выключить его, остальное вернуть", "PrimaryButton", "power", tokens, "accent_text")
        self.restore = button("Вернуть всё как было", None, "undo", tokens, "text")
        self.auto_stop = button("Остановить автопоиск", "GhostButton", "square", tokens, "text_muted")
        self.auto_stop.clicked.connect(self.auto_stop_requested)
        self.yes.clicked.connect(lambda: self.answered.emit(True))
        self.no.clicked.connect(lambda: self.answered.emit(False))
        self.stop.clicked.connect(self.cancel_requested)
        self.keep.clicked.connect(lambda: self.finish_requested.emit(True))
        self.restore.clicked.connect(lambda: self.finish_requested.emit(False))
        for b in (self.yes, self.no, self.stop, self.keep, self.restore, self.auto_stop):
            layout.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter)
        self.auto_stop.hide()
        self.hide()

    def show_step(self, step: int, total_steps: int, disabled: int, suspects: int):
        self.text.setText(f"<b>Поиск проблемного мода — шаг {step} из ~{total_steps}.</b> Выключено модов: {disabled}, "
                          f"подозреваемых: {suspects}.<br>Запустите игру так же, как при ошибке, и ответьте, "
                          "осталась ли проблема.")
        for b in (self.yes, self.no, self.stop):
            b.show()
        for b in (self.keep, self.restore, self.auto_stop):
            b.hide()
        self.show()

    def show_auto(self, step: int, total_steps: int, disabled: int, suspects: int, status: str):
        self.text.setText(f"<b>Автоматический поиск — шаг {step} из ~{total_steps}.</b> Выключено модов: {disabled}, "
                          f"подозреваемых: {suspects}.<br>Сейчас: {status} Ничего нажимать не нужно.")
        for b in (self.yes, self.no, self.stop, self.keep, self.restore):
            b.hide()
        self.auto_stop.show()
        self.show()

    def show_result(self, name: str):
        self.text.setText(f"<b>Виновник найден: {name}.</b> Выключите его — остальные моды вернутся в прежнее состояние.")
        for b in (self.yes, self.no, self.stop, self.auto_stop):
            b.hide()
        for b in (self.keep, self.restore):
            b.show()
        self.show()


# --- Сравнение сборок ---

class CompareDialog(FramelessDialog):
    def __init__(self, parent, tokens: dict, left_name: str, right_name: str, diff: Dict[str, List]):
        super().__init__(parent, "Сравнение сборок", 720)
        self.resize(780, 620)
        summary = (f"«{left_name}» и «{right_name}»: совпадают {len(diff['same'])}, отличаются версии у "
                   f"{len(diff['different'])}, только в одной — {len(diff['only_left'])} и {len(diff['only_right'])}.")
        self.body.addWidget(label(summary, "Muted", wrap=True))
        text = QPlainTextEdit()
        text.setReadOnly(True)
        lines = []

        def section(title, items):
            lines.append(f"── {title} ({len(items)}) ──")
            lines.extend(items or ["  —"])
            lines.append("")

        section(f"Только в «{left_name}»", [f"  + {e.get('name')}  {e.get('current_version') or ''}" for e in diff["only_left"]])
        section(f"Только в «{right_name}»", [f"  + {e.get('name')}  {e.get('current_version') or ''}" for e in diff["only_right"]])
        section("Разные версии", [f"  ≠ {a.get('name')}:  {a.get('current_version') or '?'}  ↔  {b.get('current_version') or '?'}"
                                  for a, b in diff["different"]])
        text.setPlainText("\n".join(lines))
        self.body.addWidget(text, 1)
        self.add_button("Закрыть", True, self.accept)
        self.finish_layout()


# --- Проверка целостности ---

class IntegrityDialog(FramelessDialog):
    fix_requested = pyqtSignal()

    def __init__(self, parent, tokens: dict, result: Dict[str, List[str]]):
        super().__init__(parent, "Проверка целостности", 560)
        problems = sum(len(v) for v in result.values())
        if not problems:
            self.body.addWidget(label("✓ Все файлы в порядке: архивы открываются, остатков загрузки нет.", None, wrap=True))
        else:
            parts = []
            for key, title in (("broken", "Повреждённые архивы — игра на них упадёт"), ("empty", "Пустые файлы"),
                               ("partial", "Остатки прерванной загрузки")):
                if result[key]:
                    parts.append(f"<b>{title}:</b><br>" + "<br>".join(f"&nbsp;&nbsp;• {os.path.basename(p)}" for p in result[key]))
            info = label("<br><br>".join(parts), None, wrap=True)
            info.setTextFormat(Qt.TextFormat.RichText)
            self.body.addWidget(info)
            self.body.addWidget(label("Исправление уберёт эти файлы в бэкап — их можно вернуть на странице «История». "
                                      "Повреждённый мод потом можно установить заново из каталога.", "Muted", wrap=True))
            self.add_button("Отмена")
            self.add_button(f"Исправить ({problems})", True, lambda: (self.fix_requested.emit(), self.accept()))
        if not problems:
            self.add_button("Закрыть", True, self.accept)
        self.finish_layout()


# --- Клонирование ---

class CloneDialog(FramelessDialog):
    def __init__(self, parent, profile_name: str, game_dir: str, existing_names=()):
        super().__init__(parent, "Клонировать сборку", 560)
        self.existing = set(existing_names)
        self.body.addWidget(label("Копия папки игры для экспериментов: моды, настройки модов, шейдеры, ресурспаки и "
                                  "options.txt. Новая сборка появится в списке слева.", "Muted", wrap=True))
        base = f"{profile_name} (копия)"
        name, n = base, 2
        while name in self.existing:
            name, n = f"{profile_name} (копия {n})", n + 1
        self.name_edit = QLineEdit(name)
        self.body.addWidget(QLabel("Название"))
        self.body.addWidget(self.name_edit)
        self.body.addWidget(QLabel("Папка новой сборки"))
        row = QHBoxLayout()
        parent_dir = os.path.dirname(os.path.normpath(game_dir)) if game_dir else ""
        self.path_edit = QLineEdit(os.path.join(parent_dir, f"{os.path.basename(os.path.normpath(game_dir))}-copy")
                                   if game_dir else "")
        browse = QPushButton("Обзор…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path_edit, 1)
        row.addWidget(browse)
        self.body.addLayout(row)
        self.saves = QCheckBox("Скопировать и миры (папка saves) — может занять много места")
        self.body.addWidget(self.saves)
        self.error = label("", "WarningText", wrap=True)
        self.error.hide()
        self.body.addWidget(self.error)
        self.add_button("Отмена")
        self.add_button("Клонировать", True, self._accept)
        self.finish_layout()

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "Папка для копии", os.path.dirname(self.path_edit.text()))
        if path:
            self.path_edit.setText(path)

    def _accept(self):
        name, path = self.name_edit.text().strip(), self.path_edit.text().strip()
        error = None
        if not name or name in self.existing:
            error = "Введите новое название сборки."
        elif not path:
            error = "Укажите папку."
        elif os.path.isdir(path) and os.listdir(path):
            error = "Эта папка не пустая — выберите другую."
        if error:
            self.error.setText(error)
            self.error.show()
            return
        self.accept()

    def values(self) -> tuple:
        return self.name_edit.text().strip(), self.path_edit.text().strip(), self.saves.isChecked()
