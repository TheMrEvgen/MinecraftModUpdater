"""Дополнительные страницы: история изменений, журнал, настройки."""
from typing import Dict, List

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import (
    QButtonGroup, QCheckBox, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QRadioButton,
    QScrollArea, QComboBox, QVBoxLayout, QWidget,
)

import icons
from ui.widgets import EmptyState, button, label, refresh_button_icons
from updater import KIND_LABELS, SNAPSHOT_REASONS
from view_model import format_size

KIND_ICONS = {"update": "download", "delete": "trash", "replace_all": "layers", "add": "plus", "restore": "camera",
              "migrate": "trending_up"}


def page_header(title: str, subtitle: str) -> QVBoxLayout:
    box = QVBoxLayout()
    box.setSpacing(4)
    box.addWidget(label(title, "PageTitle"))
    box.addWidget(label(subtitle, "PageSubtitle", wrap=True))
    return box


class HistoryPage(QWidget):
    rollback_requested = pyqtSignal(object)
    open_backups_requested = pyqtSignal()
    snapshot_create_requested = pyqtSignal()
    snapshot_restore_requested = pyqtSignal(object)
    snapshot_delete_requested = pyqtSignal(object)

    def __init__(self, tokens: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.tokens = tokens
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 24)
        root.setSpacing(18)
        head = QHBoxLayout()
        head.addLayout(page_header("История изменений",
                                   "Снимки сохраняют весь набор модов целиком, а каждая операция — заменённые файлы. "
                                   "К любому снимку и любую операцию можно вернуть."), 1)
        self.snapshot_btn = button("Создать снимок", "PrimaryButton", "camera", tokens, "accent_text")
        self.snapshot_btn.clicked.connect(self.snapshot_create_requested)
        self.open_btn = button("Папка бэкапов", None, "folder", tokens, "text")
        self.open_btn.clicked.connect(self.open_backups_requested)
        head.addWidget(self.snapshot_btn, 0, Qt.AlignmentFlag.AlignTop)
        head.addWidget(self.open_btn, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(head)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.list_layout = QVBoxLayout(self.container)
        self.list_layout.setContentsMargins(0, 0, 8, 0)
        self.list_layout.setSpacing(10)
        scroll.setWidget(self.container)
        root.addWidget(scroll, 1)

    def set_tokens(self, tokens: dict):
        self.tokens = tokens
        refresh_button_icons(self, tokens)

    def set_transactions(self, transactions: List[Dict], snapshots: List[Dict] = ()):
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not transactions and not snapshots:
            empty = EmptyState()
            empty.set_content(self.tokens, "history", "Пока пусто",
                              "Здесь появятся снимки сборки, обновления, установки и удаления модов.")
            self.list_layout.addWidget(empty)
            self.list_layout.addStretch()
            return
        if snapshots:
            self.list_layout.addWidget(label("СНИМКИ СБОРКИ", "SectionLabel"))
            for snapshot in snapshots:
                self.list_layout.addWidget(self._snapshot_card(snapshot))
            self.list_layout.addSpacing(10)
        if transactions:
            self.list_layout.addWidget(label("ОПЕРАЦИИ", "SectionLabel"))
        for i, manifest in enumerate(transactions):
            self.list_layout.addWidget(self._card(manifest, latest=(i == 0)))
        self.list_layout.addStretch()

    def _snapshot_card(self, snapshot: Dict) -> QFrame:
        t = self.tokens
        card = QFrame()
        card.setObjectName("Card")
        lay = QHBoxLayout(card)
        lay.setContentsMargins(16, 12, 14, 12)
        lay.setSpacing(14)
        icon_label = QLabel()
        icon_label.setFixedSize(40, 40)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setPixmap(icons.pixmap("camera", t["info"], 20))
        icon_label.setStyleSheet(f"background: {t['surface2']}; border-radius: 10px;")
        lay.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(3)
        reason = snapshot.get("title") or SNAPSHOT_REASONS.get(snapshot.get("reason"), snapshot.get("reason", ""))
        files = snapshot.get("files") or []
        title_label = label(f"<b>Снимок: {len(files)} файлов</b>"
                            f"   <span style='color:{t['text_faint']}'>{format_size(snapshot.get('total_size') or 0)}</span>")
        title_label.setTextFormat(Qt.TextFormat.RichText)
        text.addWidget(title_label)
        text.addWidget(label(f"{snapshot.get('created', '').replace('T', '  ')}   ·   {reason}", "Faint"))
        names = [f.get("name") for f in files]
        files_label = label(", ".join(names[:4]) + (f" и ещё {len(names) - 4}" if len(names) > 4 else ""), "Muted", wrap=True)
        files_label.setToolTip("\n".join(names))
        text.addWidget(files_label)
        lay.addLayout(text, 1)
        restore = button("Восстановить", None, "undo", t, "text")
        restore.setToolTip("Вернуть папку к этому набору модов (текущие файлы уйдут в бэкап — это тоже можно отменить)")
        restore.clicked.connect(lambda: self.snapshot_restore_requested.emit(snapshot))
        delete = button("", "IconButton", "trash", t, "text_muted", "Удалить снимок")
        delete.setFixedSize(34, 34)
        delete.clicked.connect(lambda: self.snapshot_delete_requested.emit(snapshot))
        lay.addWidget(restore, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(delete, 0, Qt.AlignmentFlag.AlignVCenter)
        return card

    def _card(self, manifest: Dict, latest: bool) -> QFrame:
        t = self.tokens
        card = QFrame()
        card.setObjectName("Card")
        lay = QHBoxLayout(card)
        lay.setContentsMargins(16, 14, 14, 14)
        lay.setSpacing(14)
        kind = manifest.get("kind", "update")
        icon_label = QLabel()
        icon_label.setFixedSize(40, 40)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setPixmap(icons.pixmap(KIND_ICONS.get(kind, "history"), t["accent"], 20))
        icon_label.setStyleSheet(f"background: {t['surface2']}; border-radius: 10px;")
        lay.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(3)
        installed, backed_up = manifest.get("installed", []), manifest.get("backed_up", [])
        title = KIND_LABELS.get(kind, kind).capitalize()
        badge = f"   <span style='color:{t['accent']}; font-size:8pt'>● последняя</span>" if latest else ""
        title_label = label(f"<b>{title}</b>{badge}")
        title_label.setTextFormat(Qt.TextFormat.RichText)
        text.addWidget(title_label)
        stats = []
        if installed:
            stats.append(f"установлено: {len(installed)}")
        if backed_up:
            stats.append(f"сохранено в бэкап: {len(backed_up)}")
        text.addWidget(label(f"{manifest.get('created', '').replace('T', '  ')}   ·   {', '.join(stats)}", "Faint"))
        names = installed or [e.get("name") for e in backed_up]
        preview = ", ".join(n for n in names[:4] if n)
        if len(names) > 4:
            preview += f" и ещё {len(names) - 4}"
        files_label = label(preview, "Muted", wrap=True)
        files_label.setToolTip("\n".join(
            [f"+ {n}" for n in installed] + [f"↺ {e.get('name')}" for e in backed_up]))
        text.addWidget(files_label)
        lay.addLayout(text, 1)

        rollback = button("Вернуть", None, "undo", t, "text")
        rollback.setToolTip("Удалить файлы, установленные этой операцией, и вернуть прежние")
        rollback.clicked.connect(lambda: self.rollback_requested.emit(manifest))
        lay.addWidget(rollback, 0, Qt.AlignmentFlag.AlignVCenter)
        return card


class LogPage(QWidget):
    open_logs_requested = pyqtSignal()

    def __init__(self, tokens: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 24)
        root.setSpacing(16)
        head = QHBoxLayout()
        head.addLayout(page_header("Журнал", "Подробный ход операций. Пригодится, если что-то пошло не так."), 1)
        self.copy_btn = button("Копировать", None, "copy", tokens, "text")
        self.folder_btn = button("Папка логов", None, "folder", tokens, "text")
        self.clear_btn = button("Очистить", "GhostButton", "x", tokens, "text_muted")
        for b in (self.copy_btn, self.folder_btn, self.clear_btn):
            head.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(head)
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(5000)
        self.console.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        mono.setPointSizeF(9)
        self.console.setFont(mono)
        root.addWidget(self.console, 1)
        self.copy_btn.clicked.connect(self._copy)
        self.clear_btn.clicked.connect(self.console.clear)
        self.folder_btn.clicked.connect(self.open_logs_requested)

    def _copy(self):
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.console.toPlainText())

    def set_tokens(self, tokens: dict):
        refresh_button_icons(self, tokens)


class SettingsPage(QWidget):
    theme_changed = pyqtSignal(str)
    settings_changed = pyqtSignal(dict)
    discover_requested = pyqtSignal()
    clear_cache_requested = pyqtSignal()
    open_data_requested = pyqtSignal()

    def __init__(self, tokens: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.tokens = tokens
        self._loading = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        root = QVBoxLayout(content)
        root.setContentsMargins(32, 28, 32, 24)
        root.setSpacing(16)
        root.addLayout(page_header("Настройки", "Оформление, проверка обновлений и данные приложения."))

        # Оформление
        card, body = self._card("Оформление")
        theme_row = QHBoxLayout()
        self.theme_group = QButtonGroup(self)
        for i, (key, text) in enumerate((("system", "Как в системе"), ("dark", "Тёмная"), ("light", "Светлая"))):
            radio = QRadioButton(text)
            radio.setProperty("theme_key", key)
            self.theme_group.addButton(radio, i)
            theme_row.addWidget(radio)
        theme_row.addStretch()
        self.theme_group.buttonClicked.connect(lambda b: not self._loading and self.theme_changed.emit(b.property("theme_key")))
        body.addLayout(theme_row)
        root.addWidget(card)

        # Список модов
        card, body = self._card("Список модов")
        body.addWidget(label("Как показывать моды сборки.",
                             "Muted", wrap=True))
        self.view_group = QButtonGroup(self)
        for i, (key, text, hint) in enumerate((
                ("flat", "Все моды и зависимости", "установленные моды и недостающие зависимости одним списком"),
                ("mine", "Только мои моды", "только файлы из папки mods, без недостающих зависимостей"),
                ("tree", "Дерево с зависимостями", "у каждого мода раскрывается список его зависимостей"))):
            radio = QRadioButton(text)
            radio.setProperty("view_key", key)
            self.view_group.addButton(radio, i)
            body.addWidget(radio)
            hint_label = label(hint[:1].upper() + hint[1:], "Faint", wrap=True)
            hint_label.setContentsMargins(26, 0, 0, 4)
            body.addWidget(hint_label)
        self.view_group.buttonClicked.connect(self._emit)
        self.expand_deps = QCheckBox("В дереве сразу раскрывать зависимости")
        self.expand_deps.toggled.connect(self._emit)
        self.compact_rows = QCheckBox("Компактные строки (без описания, больше модов на экране)")
        self.compact_rows.toggled.connect(self._emit)
        self.details_on_select = QCheckBox("Открывать панель мода сбоку при выборе строки "
                                           "(иначе — двойной щелчок, Enter или «Подробнее» в меню)")
        self.details_on_select.toggled.connect(self._emit)
        self.group_by_status = QCheckBox("Заголовки групп («Обновления», «Актуальные»…) при сортировке «Сначала важные»")
        self.group_by_status.toggled.connect(self._emit)
        self.animations = QCheckBox("Анимации (плавное раскрытие зависимостей)")
        self.animations.toggled.connect(self._emit)
        for widget in (self.expand_deps, self.compact_rows, self.details_on_select, self.group_by_status, self.animations):
            body.addWidget(widget)
        root.addWidget(card)

        # Обновления
        card, body = self._card("Проверка обновлений")
        self.auto_check = QCheckBox("Проверять обновления автоматически при открытии сборки")
        self.auto_check.toggled.connect(self._emit)
        body.addWidget(self.auto_check)
        self.changelog_before = QCheckBox("Показывать список изменений перед «Обновить все»")
        self.changelog_before.toggled.connect(self._emit)
        body.addWidget(self.changelog_before)
        row = QHBoxLayout()
        row.addWidget(label("Одновременных загрузок:"))
        self.parallel = QComboBox()
        self.parallel.addItems([str(i) for i in range(1, 7)])
        self.parallel.currentIndexChanged.connect(self._emit)
        row.addWidget(self.parallel)
        row.addStretch()
        body.addLayout(row)
        root.addWidget(card)

        # Снимки
        card, body = self._card("Снимки сборки")
        body.addWidget(label("Снимок сохраняет весь набор модов, чтобы вернуться к нему со страницы «История». "
                             "Файлы связываются жёсткими ссылками и почти не занимают места.", "Muted", wrap=True))
        self.auto_snapshot = QCheckBox("Делать снимок перед «Обновить все», заменой сборки и сменой версии игры")
        self.auto_snapshot.toggled.connect(self._emit)
        body.addWidget(self.auto_snapshot)
        row = QHBoxLayout()
        row.addWidget(label("Хранить автоматических снимков:"))
        self.snapshots_keep = QComboBox()
        self.snapshots_keep.addItems(["3", "5", "10", "20", "50"])
        self.snapshots_keep.currentIndexChanged.connect(self._emit)
        row.addWidget(self.snapshots_keep)
        row.addStretch()
        body.addLayout(row)
        root.addWidget(card)

        # Лаунчеры
        card, body = self._card("Лаунчеры")
        body.addWidget(label("Сборки из Prism Launcher, MultiMC, Modrinth App, CurseForge и официального "
                             "лаунчера добавляются в список автоматически.", "Muted", wrap=True))
        self.discover = QCheckBox("Искать сборки при запуске")
        self.discover.toggled.connect(self._emit)
        body.addWidget(self.discover)
        self.discover_btn = button("Найти сборки сейчас", None, "box_search", tokens, "text")
        self.discover_btn.clicked.connect(self.discover_requested)
        body.addWidget(self.discover_btn, 0, Qt.AlignmentFlag.AlignLeft)
        root.addWidget(card)

        # Данные
        card, body = self._card("Данные")
        body.addWidget(label("Кэш хранит хэши файлов, информацию о модах и иконки — с ним проверка "
                             "занимает секунды. Очистка безопасна.", "Muted", wrap=True))
        row = QHBoxLayout()
        self.clear_btn = button("Очистить кэш", None, "trash", tokens, "text")
        self.clear_btn.clicked.connect(self.clear_cache_requested)
        self.data_btn = button("Папка данных", None, "folder", tokens, "text")
        self.data_btn.clicked.connect(self.open_data_requested)
        row.addWidget(self.clear_btn)
        row.addWidget(self.data_btn)
        row.addStretch()
        body.addLayout(row)
        root.addWidget(card)

        # Горячие клавиши
        card, body = self._card("Горячие клавиши")
        shortcuts = label("<table cellspacing='4'>"
                          "<tr><td><b>F5</b></td><td>&nbsp;&nbsp;проверить обновления</td></tr>"
                          "<tr><td><b>Ctrl+K</b></td><td>&nbsp;&nbsp;быстрые команды: действия, сборки, моды</td></tr>"
                          "<tr><td><b>Ctrl+F</b></td><td>&nbsp;&nbsp;поиск</td></tr>"
                          "<tr><td><b>Пробел</b></td><td>&nbsp;&nbsp;отметить мод</td></tr>"
                          "<tr><td><b>Enter</b></td><td>&nbsp;&nbsp;открыть панель мода</td></tr>"
                          "<tr><td><b>→ / ←</b></td><td>&nbsp;&nbsp;раскрыть / свернуть зависимости (дерево)</td></tr>"
                          "<tr><td><b>Delete</b></td><td>&nbsp;&nbsp;удалить отмеченные</td></tr>"
                          "<tr><td><b>Esc</b></td><td>&nbsp;&nbsp;закрыть панель / сбросить поиск</td></tr>"
                          "<tr><td><b>Перетаскивание</b></td><td>&nbsp;&nbsp;.jar, .mrpack или строка сборки в окно</td></tr>"
                          "</table>", "Muted")
        shortcuts.setTextFormat(Qt.TextFormat.RichText)
        body.addWidget(shortcuts)
        root.addWidget(card)
        root.addStretch()

    def _card(self, title: str):
        card = QFrame()
        card.setObjectName("Card")
        body = QVBoxLayout(card)
        body.setContentsMargins(18, 16, 18, 16)
        body.setSpacing(10)
        body.addWidget(label(title, "SectionTitle"))
        return card, body

    def set_tokens(self, tokens: dict):
        self.tokens = tokens
        refresh_button_icons(self, tokens)

    def load(self, theme: str, settings: Dict):
        self._loading = True
        for b in self.theme_group.buttons():
            b.setChecked(b.property("theme_key") == theme)
        self.auto_check.setChecked(bool(settings.get("auto_check", True)))
        self.discover.setChecked(bool(settings.get("discover_launchers", True)))
        self.parallel.setCurrentText(str(int(settings.get("parallel_downloads", 3))))
        for b in self.view_group.buttons():
            b.setChecked(b.property("view_key") == settings.get("view_mode", "flat"))
        self.expand_deps.setChecked(bool(settings.get("expand_dependencies", False)))
        self.compact_rows.setChecked(bool(settings.get("compact_rows", False)))
        self.details_on_select.setChecked(bool(settings.get("details_on_select", True)))
        self.group_by_status.setChecked(bool(settings.get("group_by_status", True)))
        self.animations.setChecked(bool(settings.get("animations", True)))
        self.changelog_before.setChecked(bool(settings.get("changelog_before_update", True)))
        self.auto_snapshot.setChecked(bool(settings.get("auto_snapshot", True)))
        keep = str(int(settings.get("snapshots_keep", 10)))
        if self.snapshots_keep.findText(keep) < 0:
            self.snapshots_keep.addItem(keep)
        self.snapshots_keep.setCurrentText(keep)
        self._loading = False

    def _emit(self, *_):
        if not self._loading:
            checked_view = self.view_group.checkedButton()
            view_mode = checked_view.property("view_key") if checked_view else "flat"
            self.settings_changed.emit({
                "view_mode": view_mode,
                "expand_dependencies": self.expand_deps.isChecked(),
                "compact_rows": self.compact_rows.isChecked(),
                "details_on_select": self.details_on_select.isChecked(),
                "group_by_status": self.group_by_status.isChecked(),
                "animations": self.animations.isChecked(),
                "changelog_before_update": self.changelog_before.isChecked(),
                "auto_snapshot": self.auto_snapshot.isChecked(),
                "snapshots_keep": int(self.snapshots_keep.currentText()),
                "auto_check": self.auto_check.isChecked(),
                "discover_launchers": self.discover.isChecked(),
                "parallel_downloads": int(self.parallel.currentText()),
            })
