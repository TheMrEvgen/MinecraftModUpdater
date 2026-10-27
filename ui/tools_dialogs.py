"""Инструменты: разбор краша, поиск проблемного мода, сравнение сборок, проверка целостности, клонирование."""
import os
from typing import Callable, Dict, List

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

import icons
from ui.dialogs import FramelessDialog
from ui.widgets import button, label
from i18n import tr


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
    fix_requested = pyqtSignal(str)  # "java" | "memory" | "loader:<версия>" — исправить в лаунчере

    def __init__(self, parent, tokens: dict, files: List[str], entries: List[Dict], run_task: Callable,
                 preferred: str = None, fixable: tuple = ()):
        super().__init__(parent, tr("Почему упала игра"), 680)
        self.resize(760, 620)
        self.tokens = tokens
        self.entries = entries
        self.run_task = run_task
        self.fixable = fixable
        self.body.addWidget(label(tr("Программа читает последний краш-репорт или лог игры и ищет моды, которые "
                                  "упоминаются в ошибке. Это подсказка, а не приговор: виновника надёжнее всего "
                                  "находит поиск перебором."), "Muted", wrap=True))
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Файл:")))
        self.file_combo = QComboBox()
        for path in files:
            name = os.path.basename(path)
            self.file_combo.addItem(name if name != "latest.log" else tr("latest.log — лог последнего запуска"), path)
        if preferred:
            index = self.file_combo.findData(preferred)
            if index < 0 and os.path.isfile(preferred):
                self.file_combo.insertItem(0, os.path.basename(preferred), preferred)
                index = 0
            self.file_combo.setCurrentIndex(max(0, index))
        self.file_combo.currentIndexChanged.connect(self._analyze)
        row.addWidget(self.file_combo, 1)
        self.open_btn = button("", "IconButton", "external", tokens, "text_muted", tr("Открыть файл"))
        self.open_btn.clicked.connect(self._open_file)
        row.addWidget(self.open_btn)
        self.body.addLayout(row)

        # Вылет не из-за мода (Java, память) — главное в отчёте, поэтому сверху
        self.diagnosis = QVBoxLayout()
        self.diagnosis.setSpacing(6)
        self.body.addLayout(self.diagnosis)

        self.summary = QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setFixedHeight(120)
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        mono.setPointSizeF(9)
        self.summary.setFont(mono)
        self.body.addWidget(self.summary)
        self.body.addWidget(label(tr("Подозреваемые моды"), "SectionTitle"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        self.suspects = QVBoxLayout(holder)
        self.suspects.setContentsMargins(0, 0, 0, 0)
        self.suspects.setSpacing(6)
        scroll.setWidget(holder)
        self.body.addWidget(scroll, 1)

        bisect = self.add_button(tr("Найти виновника перебором…"), False, lambda: (self.bisect_requested.emit(), self.accept()))
        bisect.setToolTip(tr("Программа будет выключать половину модов, а вы — проверять запуск игры"))
        self.add_button(tr("Закрыть"), True, self.accept)
        self.finish_layout()
        if files:
            self._analyze()
        else:
            self.summary.setPlainText(tr("Краш-репортов и лога нет: игра с этой сборкой ещё не запускалась."))

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
        self.summary.setPlainText(tr("Разбираем…"))
        _clear(self.suspects)
        self.run_task(analyze_file, (path, self.entries), self._show, lambda e: self.summary.setPlainText(tr('Ошибка: {0}', e)),
                      None)

    def _show(self, report):
        t = self.tokens
        self.summary.setPlainText("\n".join(report.summary) or tr("В файле не найдено описания ошибки."))
        _clear(self.suspects)
        _clear(self.diagnosis)
        for item in report.diagnosis:
            card = QFrame()
            card.setObjectName("Banner")
            row = QHBoxLayout(card)
            row.setContentsMargins(12, 8, 8, 8)
            hint = (tr("Выключать моды не нужно — обновите загрузчик.") if item.kind == "loader"
                    else tr("Моды тут, скорее всего, ни при чём."))
            text = label(f"<b>{item.text}</b><br>{hint}", None, wrap=True)
            text.setTextFormat(Qt.TextFormat.RichText)
            row.addWidget(text, 1)
            if item.kind in self.fixable:
                caption = {"memory": tr("Выделить больше памяти"), "java": tr("Выбирать Java автоматически"),
                           "loader": tr("Обновить загрузчик до {0}", item.version)}[item.kind]
                fix = button(caption, "Primary")
                request = f"loader:{item.version}" if item.kind == "loader" else item.kind
                fix.clicked.connect(lambda _=False, r=request: (self.fix_requested.emit(r), self.accept()))
                row.addWidget(fix)
            self.diagnosis.addWidget(card)
        if any(item.kind == "loader" for item in report.diagnosis):  # Моды исправны — им нужен загрузчик новее
            self.suspects.addWidget(label(tr("Моды, которые названы в ошибке, исправны — им нужен загрузчик новее. "
                                             "Выключать их не нужно."), "Muted", wrap=True))
            self.suspects.addStretch()
            return
        if not report.suspects:
            self.suspects.addWidget(label(tr("Ни один мод не упоминается явно. Попробуйте поиск перебором."), "Muted", wrap=True))
        top = report.suspects[0].score if report.suspects else 1
        for suspect in report.suspects:
            card = QFrame()
            card.setObjectName("Card")
            row = QHBoxLayout(card)
            row.setContentsMargins(12, 8, 8, 8)
            level = tr("высокая") if suspect.score >= top * 0.6 else tr("средняя") if suspect.score >= top * 0.3 else tr("низкая")
            color = t["danger"] if level == tr("высокая") else t["warning"] if level == tr("средняя") else t["text_muted"]
            text = label(tr("<b>{0}</b>   <span style='color:{1}'>● вероятность {2}</span><br><span style='color:{3}'>{4}</span>", suspect.entry.get('name'), color, level, t['text_faint'], '; '.join(suspect.reasons)), None, wrap=True)
            text.setTextFormat(Qt.TextFormat.RichText)
            row.addWidget(text, 1)
            show = button("", "IconButton", "search", t, "text_muted", tr("Показать в списке"))
            show.clicked.connect(lambda _=False, e=suspect.entry: (self.show_requested.emit(e), self.accept()))
            row.addWidget(show)
            if not suspect.entry.get("disabled"):
                off = button(tr("Выключить"), None, "power", t, "text")
                off.clicked.connect(lambda _=False, e=suspect.entry: self._disable(e))
                row.addWidget(off)
            self.suspects.addWidget(card)
        self.suspects.addStretch()

    def _disable(self, entry: Dict):
        self.disable_requested.emit(entry)
        self._show_after_disable(entry)

    def _show_after_disable(self, entry: Dict):
        self.summary.appendPlainText(tr('\n→ «{0}» выключен. Запустите игру и проверьте.', entry.get('name')))


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
        self.yes = button(tr("Проблема осталась"), "DangerButton", "alert", tokens, "danger")
        self.no = button(tr("Всё работает"), "PrimaryButton", "check", tokens, "accent_text")
        self.stop = button(tr("Прервать"), "GhostButton", "x", tokens, "text_muted", tr("Вернуть все моды как было"))
        self.keep = button(tr("Выключить его, остальное вернуть"), "PrimaryButton", "power", tokens, "accent_text")
        self.restore = button(tr("Вернуть всё как было"), None, "undo", tokens, "text")
        self.auto_stop = button(tr("Остановить автопоиск"), "GhostButton", "square", tokens, "text_muted")
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
        self.text.setText(tr('<b>Поиск проблемного мода — шаг {0} из ~{1}.</b> Выключено модов: {2}, подозреваемых: {3}.<br>Запустите игру так же, как при ошибке, и ответьте, осталась ли проблема.', step, total_steps, disabled, suspects))
        for b in (self.yes, self.no, self.stop):
            b.show()
        for b in (self.keep, self.restore, self.auto_stop):
            b.hide()
        self.show()

    def show_auto(self, step: int, total_steps: int, disabled: int, suspects: int, status: str):
        self.text.setText(tr('<b>Автоматический поиск — шаг {0} из ~{1}.</b> Выключено модов: {2}, подозреваемых: {3}.<br>Сейчас: {4} Ничего нажимать не нужно.', step, total_steps, disabled, suspects, status))
        for b in (self.yes, self.no, self.stop, self.keep, self.restore):
            b.hide()
        self.auto_stop.show()
        self.show()

    def show_result(self, name: str):
        self.text.setText(tr('<b>Виновник найден: {0}.</b> Выключите его — остальные моды вернутся в прежнее состояние.', name))
        for b in (self.yes, self.no, self.stop, self.auto_stop):
            b.hide()
        for b in (self.keep, self.restore):
            b.show()
        self.show()


# --- Сравнение сборок ---

class CompareDialog(FramelessDialog):
    def __init__(self, parent, tokens: dict, left_name: str, right_name: str, diff: Dict[str, List]):
        super().__init__(parent, tr("Сравнение сборок"), 720)
        self.resize(780, 620)
        summary = (tr('«{0}» и «{1}»: совпадают {2}, отличаются версии у {3}, только в одной — {4} и {5}.', left_name, right_name, len(diff['same']), len(diff['different']), len(diff['only_left']), len(diff['only_right'])))
        self.body.addWidget(label(summary, "Muted", wrap=True))
        text = QPlainTextEdit()
        text.setReadOnly(True)
        lines = []

        def section(title, items):
            lines.append(f"── {title} ({len(items)}) ──")
            lines.extend(items or ["  —"])
            lines.append("")

        section(tr('Только в «{0}»', left_name), [f"  + {e.get('name')}  {e.get('current_version') or ''}" for e in diff["only_left"]])
        section(tr('Только в «{0}»', right_name), [f"  + {e.get('name')}  {e.get('current_version') or ''}" for e in diff["only_right"]])
        section(tr("Разные версии"), [f"  ≠ {a.get('name')}:  {a.get('current_version') or '?'}  ↔  {b.get('current_version') or '?'}"
                                  for a, b in diff["different"]])
        text.setPlainText("\n".join(lines))
        self.body.addWidget(text, 1)
        self.add_button(tr("Закрыть"), True, self.accept)
        self.finish_layout()


# --- Проверка целостности ---

class IntegrityDialog(FramelessDialog):
    fix_requested = pyqtSignal()

    def __init__(self, parent, tokens: dict, result: Dict[str, List[str]]):
        super().__init__(parent, tr("Проверка целостности"), 560)
        problems = sum(len(v) for v in result.values())
        if not problems:
            self.body.addWidget(label(tr("✓ Все файлы в порядке: архивы открываются, остатков загрузки нет."), None, wrap=True))
        else:
            parts = []
            for key, title in (("broken", tr("Повреждённые архивы — игра на них упадёт")), ("empty", tr("Пустые файлы")),
                               ("partial", tr("Остатки прерванной загрузки"))):
                if result[key]:
                    parts.append(f"<b>{title}:</b><br>" + "<br>".join(f"&nbsp;&nbsp;• {os.path.basename(p)}" for p in result[key]))
            info = label("<br><br>".join(parts), None, wrap=True)
            info.setTextFormat(Qt.TextFormat.RichText)
            self.body.addWidget(info)
            self.body.addWidget(label(tr("Исправление уберёт эти файлы в бэкап — их можно вернуть на странице «История». "
                                      "Повреждённый мод потом можно установить заново из каталога."), "Muted", wrap=True))
            self.add_button(tr("Отмена"))
            self.add_button(tr('Исправить ({0})', problems), True, lambda: (self.fix_requested.emit(), self.accept()))
        if not problems:
            self.add_button(tr("Закрыть"), True, self.accept)
        self.finish_layout()


# --- Сборка для сервера ---

class ServerPackDialog(FramelessDialog):
    """Какие моды положить в сборку для сервера: «только для клиента» уже сняты, остальное можно поправить."""

    def __init__(self, parent, server: List[Dict], client: List[Dict]):
        super().__init__(parent, tr("Сборка для сервера"), 560)
        self.resize(600, 620)
        unknown = sum(1 for e in server if not e.get("server_side"))
        text = tr("В архив попадут моды, настройки (config) и всё для запуска сервера. Моды «только для клиента» "
                  "по данным Modrinth уже сняты: на сервере они не нужны, а некоторые его роняют.")
        if unknown:
            text += " " + tr("Для {0} модов сторона неизвестна (не с Modrinth) — они оставлены.", unknown)
        self.body.addWidget(label(text, "Muted", wrap=True))
        self.list = QListWidget()
        self.entries = sorted(server, key=lambda e: (e.get("name") or "").lower()) + \
            sorted(client, key=lambda e: (e.get("name") or "").lower())
        client_ids = {id(e) for e in client}
        for entry in self.entries:
            side = tr("только клиент") if id(entry) in client_ids else "" if entry.get("server_side") else tr("сторона неизвестна")
            item = QListWidgetItem(f"{entry.get('name')}" + (f"   ·   {side}" if side else ""))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if id(entry) in client_ids else Qt.CheckState.Checked)
            self.list.addItem(item)
        self.body.addWidget(self.list, 1)
        self.add_button(tr("Отмена"))
        self.ok = self.add_button("", True, self.accept)
        self.list.itemChanged.connect(lambda _: self._update())
        self.finish_layout()
        self._update()

    def selected(self) -> List[Dict]:
        return [e for i, e in enumerate(self.entries) if self.list.item(i).checkState() == Qt.CheckState.Checked]

    def skipped(self) -> List[Dict]:
        return [e for i, e in enumerate(self.entries) if self.list.item(i).checkState() != Qt.CheckState.Checked]

    def _update(self):
        count = len(self.selected())
        self.ok.setText(tr("Собрать архив ({0})", count))
        self.ok.setEnabled(bool(count))


# --- Клонирование ---

class CloneDialog(FramelessDialog):
    def __init__(self, parent, profile_name: str, game_dir: str, existing_names=()):
        super().__init__(parent, tr("Клонировать сборку"), 560)
        self.existing = set(existing_names)
        self.body.addWidget(label(tr("Копия папки игры для экспериментов: моды, настройки модов, шейдеры, ресурспаки и "
                                  "options.txt. Новая сборка появится в списке слева."), "Muted", wrap=True))
        base = tr('{0} (копия)', profile_name)
        name, n = base, 2
        while name in self.existing:
            name, n = tr('{0} (копия {1})', profile_name, n), n + 1
        self.name_edit = QLineEdit(name)
        self.body.addWidget(QLabel(tr("Название")))
        self.body.addWidget(self.name_edit)
        self.body.addWidget(QLabel(tr("Папка новой сборки")))
        row = QHBoxLayout()
        parent_dir = os.path.dirname(os.path.normpath(game_dir)) if game_dir else ""
        self.path_edit = QLineEdit(os.path.join(parent_dir, f"{os.path.basename(os.path.normpath(game_dir))}-copy")
                                   if game_dir else "")
        browse = QPushButton(tr("Обзор…"))
        browse.clicked.connect(self._browse)
        row.addWidget(self.path_edit, 1)
        row.addWidget(browse)
        self.body.addLayout(row)
        self.saves = QCheckBox(tr("Скопировать и миры (папка saves) — может занять много места"))
        self.body.addWidget(self.saves)
        self.error = label("", "WarningText", wrap=True)
        self.error.hide()
        self.body.addWidget(self.error)
        self.add_button(tr("Отмена"))
        self.add_button(tr("Клонировать"), True, self._accept)
        self.finish_layout()

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, tr("Папка для копии"), os.path.dirname(self.path_edit.text()))
        if path:
            self.path_edit.setText(path)

    def _accept(self):
        name, path = self.name_edit.text().strip(), self.path_edit.text().strip()
        error = None
        if not name or name in self.existing:
            error = tr("Введите новое название сборки.")
        elif not path:
            error = tr("Укажите папку.")
        elif os.path.isdir(path) and os.listdir(path):
            error = tr("Эта папка не пустая — выберите другую.")
        if error:
            self.error.setText(error)
            self.error.show()
            return
        self.accept()

    def values(self) -> tuple:
        return self.name_edit.text().strip(), self.path_edit.text().strip(), self.saves.isChecked()
