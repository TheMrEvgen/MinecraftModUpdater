"""Дополнительные страницы: история изменений, журнал, настройки."""
from typing import Dict, List

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFontDatabase
from PyQt6.QtWidgets import (
    QAbstractButton, QButtonGroup, QCheckBox, QColorDialog, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QRadioButton, QScrollArea, QVBoxLayout, QWidget,
)

import icons
from theme import ACCENTS, DEFAULT_ACCENT, THEME_TITLES
from ui.shortcuts import ShortcutEditor
from ui.widgets import EmptyState, SearchBox, button, label, refresh_button_icons
from updater import KIND_LABELS, SNAPSHOT_REASONS
from view_model import format_size
from i18n import LANGUAGES, language, tr

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
        head.addLayout(page_header(tr("История изменений"),
                                   tr("Снимки сохраняют весь набор модов целиком, а каждая операция — заменённые файлы. "
                                   "К любому снимку и любую операцию можно вернуть.")), 1)
        self.snapshot_btn = button(tr("Создать снимок"), "PrimaryButton", "camera", tokens, "accent_text")
        self.snapshot_btn.clicked.connect(self.snapshot_create_requested)
        self.open_btn = button(tr("Папка бэкапов"), None, "folder", tokens, "text")
        self.open_btn.clicked.connect(self.open_backups_requested)
        head.addWidget(self.snapshot_btn, 0, Qt.AlignmentFlag.AlignTop)
        head.addWidget(self.open_btn, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(head)
        self.usage_label = label("", "Faint")
        root.addWidget(self.usage_label)

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
            empty.set_content(self.tokens, "history", tr("Пока пусто"),
                              tr("Здесь появятся снимки сборки, обновления, установки и удаления модов."))
            self.list_layout.addWidget(empty)
            self.list_layout.addStretch()
            return
        if snapshots:
            self.list_layout.addWidget(label(tr("СНИМКИ СБОРКИ"), "SectionLabel"))
            for snapshot in snapshots:
                self.list_layout.addWidget(self._snapshot_card(snapshot))
            self.list_layout.addSpacing(10)
        if transactions:
            self.list_layout.addWidget(label(tr("ОПЕРАЦИИ"), "SectionLabel"))
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
        title_label = label(tr("<b>Снимок: {0} файлов</b>   <span style='color:{1}'>{2}</span>", len(files), t['text_faint'], format_size(snapshot.get('total_size') or 0)))
        title_label.setTextFormat(Qt.TextFormat.RichText)
        text.addWidget(title_label)
        text.addWidget(label(f"{snapshot.get('created', '').replace('T', '  ')}   ·   {reason}", "Faint"))
        names = [f.get("name") for f in files]
        files_label = label(", ".join(names[:4]) + (tr(' и ещё {0}', len(names) - 4) if len(names) > 4 else ""), "Muted", wrap=True)
        files_label.setToolTip("\n".join(names))
        text.addWidget(files_label)
        lay.addLayout(text, 1)
        restore = button(tr("Восстановить"), None, "undo", t, "text")
        restore.setToolTip(tr("Вернуть папку к этому набору модов (текущие файлы уйдут в бэкап — это тоже можно отменить)"))
        restore.clicked.connect(lambda: self.snapshot_restore_requested.emit(snapshot))
        delete = button("", "IconButton", "trash", t, "text_muted", tr("Удалить снимок"))
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
        badge = tr("   <span style='color:{0}; font-size:8pt'>● последняя</span>", t['accent']) if latest else ""
        title_label = label(f"<b>{title}</b>{badge}")
        title_label.setTextFormat(Qt.TextFormat.RichText)
        text.addWidget(title_label)
        stats = []
        if installed:
            stats.append(tr('установлено: {0}', len(installed)))
        if backed_up:
            stats.append(tr('сохранено в бэкап: {0}', len(backed_up)))
        text.addWidget(label(f"{manifest.get('created', '').replace('T', '  ')}   ·   {', '.join(stats)}", "Faint"))
        names = installed or [e.get("name") for e in backed_up]
        preview = ", ".join(n for n in names[:4] if n)
        if len(names) > 4:
            preview += tr(' и ещё {0}', len(names) - 4)
        files_label = label(preview, "Muted", wrap=True)
        files_label.setToolTip("\n".join(
            [f"+ {n}" for n in installed] + [f"↺ {e.get('name')}" for e in backed_up]))
        text.addWidget(files_label)
        lay.addLayout(text, 1)

        rollback = button(tr("Вернуть"), None, "undo", t, "text")
        rollback.setToolTip(tr("Удалить файлы, установленные этой операцией, и вернуть прежние"))
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
        head.addLayout(page_header(tr("Журнал"), tr("Подробный ход операций. Пригодится, если что-то пошло не так.")), 1)
        self.copy_btn = button(tr("Копировать"), None, "copy", tokens, "text")
        self.folder_btn = button(tr("Папка логов"), None, "folder", tokens, "text")
        self.clear_btn = button(tr("Очистить"), "GhostButton", "x", tokens, "text_muted")
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


SCALES = (90, 100, 110, 125, 150)
BACKUP_DAYS = ((7, tr("7 дней")), (14, tr("14 дней")), (30, tr("30 дней")), (90, tr("90 дней")), (0, tr("не удалять")))
BACKUP_MB = ((256, tr("256 МБ")), (512, tr("512 МБ")), (1024, tr("1 ГБ")), (2048, tr("2 ГБ")), (0, tr("без ограничения")))


class SettingsPage(QWidget):
    theme_changed = pyqtSignal(str)
    settings_changed = pyqtSignal(dict)
    discover_requested = pyqtSignal()
    clear_cache_requested = pyqtSignal()
    open_data_requested = pyqtSignal()
    export_requested = pyqtSignal()
    import_requested = pyqtSignal()
    prune_backups_requested = pyqtSignal()
    install_requested = pyqtSignal(dict)
    app_update_check_requested = pyqtSignal()
    autostart_toggled = pyqtSignal(bool)
    background_check_requested = pyqtSignal()
    app_update_open_requested = pyqtSignal()
    uninstall_requested = pyqtSignal()

    def __init__(self, tokens: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.tokens = tokens
        self._loading = False
        self._sections: List[tuple] = []  # (заголовок, карточка) — для навигации и поиска
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        # На очень широком экране карточки не тянутся на всю ширину — так их легче читать
        columns = QHBoxLayout(content)
        columns.setContentsMargins(0, 0, 0, 0)
        inner = QWidget()
        inner.setMaximumWidth(1280)
        columns.addWidget(inner, 1)
        columns.addStretch(0)
        root = QVBoxLayout(inner)
        root.setContentsMargins(32, 28, 32, 24)
        root.setSpacing(16)
        root.addLayout(page_header(tr("Настройки"),tr("Оформление, проверка обновлений и данные приложения.")))

        self._root = root
        self.install_card = self._build_install_card(tokens)
        root.addWidget(self.install_card)

        # Оформление
        card, body = self._card(tr("Оформление"))
        theme_row = QHBoxLayout()
        self.theme_group = QButtonGroup(self)
        for i, (key, text) in enumerate(THEME_TITLES.items()):
            radio = QRadioButton(text)
            radio.setProperty("theme_key", key)
            self.theme_group.addButton(radio, i)
            theme_row.addWidget(radio)
        theme_row.addStretch()
        self.theme_group.buttonClicked.connect(lambda b: not self._loading and self.theme_changed.emit(b.property("theme_key")))
        body.addLayout(theme_row)

        body.addWidget(label(tr("Акцентный цвет"), "Muted"))
        accent_row = QHBoxLayout()
        accent_row.setSpacing(8)
        self.accent_group = QButtonGroup(self)
        self.accent_group.setExclusive(True)
        for key, (title, dark, _light) in ACCENTS.items():
            swatch = QPushButton()
            swatch.setObjectName("Swatch")
            swatch.setCheckable(True)
            swatch.setFixedSize(30, 30)
            swatch.setToolTip(title)
            swatch.setCursor(Qt.CursorShape.PointingHandCursor)
            swatch.setProperty("accent_key", key)
            swatch.setStyleSheet(f"#Swatch {{ background: {dark}; }}")
            self.accent_group.addButton(swatch)
            accent_row.addWidget(swatch)
        self.custom_accent = QPushButton(tr("Свой цвет…"))
        self.custom_accent.setObjectName("GhostButton")
        self.custom_accent.clicked.connect(self._pick_accent)
        accent_row.addSpacing(6)
        accent_row.addWidget(self.custom_accent)
        accent_row.addStretch()
        self.accent_group.buttonClicked.connect(lambda b: self._set_accent(b.property("accent_key")))
        body.addLayout(accent_row)
        self._accent = DEFAULT_ACCENT

        row = QHBoxLayout()
        row.addWidget(label(tr("Масштаб интерфейса:")))
        self.scale = QComboBox()
        for value in SCALES:
            self.scale.addItem(f"{value}%", value)
        self.scale.currentIndexChanged.connect(self._emit)
        row.addWidget(self.scale)
        row.addWidget(label(tr("применится после перезапуска"), "Faint"))
        row.addStretch()
        body.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(label("Язык / Language:"))
        self.language = QComboBox()
        for code, title in LANGUAGES.items():
            self.language.addItem(title, code)
        self.language.currentIndexChanged.connect(self._emit)
        row.addWidget(self.language)
        row.addWidget(label(tr("применится после перезапуска"), "Faint"))
        row.addStretch()
        body.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(label(tr("Картинка в боковой панели:")))
        self.sidebar_image_btn = button(tr("Выбрать…"), None, "image", tokens, "text")
        self.sidebar_image_btn.clicked.connect(self._pick_sidebar_image)
        self.sidebar_image_clear = button(tr("Убрать"), "GhostButton", "x", tokens, "text_muted")
        self.sidebar_image_clear.clicked.connect(lambda: self._set_sidebar_image(""))
        row.addWidget(self.sidebar_image_btn)
        row.addWidget(self.sidebar_image_clear)
        row.addStretch()
        body.addLayout(row)
        self._sidebar_image = ""
        self.minimal = QCheckBox(tr("Режим «Минимум»"))
        self.minimal.toggled.connect(self._emit)
        self._add_with_hint(body, self.minimal, tr("Только список модов и большая кнопка «Обновить все»."))
        root.addWidget(card)

        # Список модов
        card, body = self._card(tr("Список модов"))
        body.addWidget(label(tr("Как показывать моды сборки."),
                             "Muted", wrap=True))
        self.view_group = QButtonGroup(self)
        for i, (key, text, hint) in enumerate((
                ("flat", tr("Все моды и зависимости"), tr("установленные моды и недостающие зависимости одним списком")),
                ("mine", tr("Только мои моды"), tr("только файлы из папки mods, без недостающих зависимостей")),
                ("tree", tr("Дерево с зависимостями"), tr("у каждого мода раскрывается список его зависимостей")))):
            radio = QRadioButton(text)
            radio.setProperty("view_key", key)
            self.view_group.addButton(radio, i)
            body.addWidget(radio)
            hint_label = label(hint[:1].upper() + hint[1:], "Faint", wrap=True)
            hint_label.setContentsMargins(26, 0, 0, 4)
            body.addWidget(hint_label)
        self.view_group.buttonClicked.connect(self._emit)
        self.expand_deps = QCheckBox(tr("В дереве сразу раскрывать зависимости"))
        self.expand_deps.toggled.connect(self._emit)
        self.compact_rows = QCheckBox(tr("Компактные строки"))
        self.compact_rows.setProperty("hint", tr("Без описания — на экране помещается больше модов."))
        self.compact_rows.toggled.connect(self._emit)
        self.details_on_select = QCheckBox(tr("Открывать панель мода при выборе строки"))
        self.details_on_select.setProperty("hint", tr("Иначе — двойной щелчок, Enter или «Подробнее» в меню мода."))
        self.details_on_select.toggled.connect(self._emit)
        self.group_by_status = QCheckBox(tr("Заголовки групп"))
        self.group_by_status.setProperty("hint", tr("«Обновления», «Актуальные»… при сортировке «Сначала важные»."))
        self.group_by_status.toggled.connect(self._emit)
        self.animations = QCheckBox(tr("Анимации"))
        self.animations.setProperty("hint", tr("Плавное раскрытие зависимостей в дереве."))
        self.animations.toggled.connect(self._emit)
        self.per_profile = QCheckBox(tr("Вид и сортировка — свои у каждой сборки"))
        self.per_profile.toggled.connect(self._emit)
        self.abandoned = QCheckBox(tr("Помечать давно не обновлявшиеся моды"))
        self.abandoned.setProperty("hint", tr("Автор не выпускал версий больше года."))
        self.abandoned.toggled.connect(self._emit)
        for widget in (self.expand_deps, self.compact_rows, self.details_on_select, self.group_by_status, self.animations,
                       self.per_profile, self.abandoned):
            self._add_with_hint(body, widget, widget.property("hint"))
        body.addWidget(label(tr("Что показывать в строке мода:"), "Muted"))
        from ui.mod_list import COLUMNS
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        self.column_boxes = {}
        for i, (key, title) in enumerate(COLUMNS):
            box = QCheckBox(title)
            box.toggled.connect(self._emit)
            self.column_boxes[key] = box
            grid.addWidget(box, i // 3, i % 3)
        grid.setColumnStretch(3, 1)  # Галочки прижаты влево, а не разбросаны по широкому экрану
        body.addLayout(grid)
        root.addWidget(card)

        # Обновления
        card, body = self._card(tr("Проверка обновлений"))
        self.auto_check = QCheckBox(tr("Проверять обновления автоматически при открытии сборки"))
        self.auto_check.toggled.connect(self._emit)
        body.addWidget(self.auto_check)
        self.changelog_before = QCheckBox(tr("Показывать «Что нового» перед «Обновить все»"))
        self.changelog_before.toggled.connect(self._emit)
        body.addWidget(self.changelog_before)
        row = QHBoxLayout()
        row.addWidget(label(tr("Ставить новые версии не раньше чем через:")))
        self.min_age = QComboBox()
        for days, title in ((0, tr("сразу")), (1, tr("1 день")), (3, tr("3 дня")), (7, tr("7 дней")), (14, tr("14 дней"))):
            self.min_age.addItem(title, days)
        self.min_age.currentIndexChanged.connect(self._emit)
        row.addWidget(self.min_age)
        row.addStretch()
        body.addLayout(row)
        hint = label(tr("В свежих версиях чаще бывают ошибки, и за несколько дней их обычно исправляют. Такие обновления "
                        "видны в списке с пометкой «ждёт», но не попадают в «Обновить все» и уведомления; "
                        "поставить вручную можно в любой момент."), "Faint", wrap=True)
        body.addWidget(hint)
        row = QHBoxLayout()
        row.addWidget(label(tr("Одновременных загрузок:")))
        self.parallel = QComboBox()
        self.parallel.addItems([str(i) for i in range(1, 7)])
        self.parallel.currentIndexChanged.connect(self._emit)
        row.addWidget(self.parallel)
        row.addStretch()
        body.addLayout(row)
        root.addWidget(card)

        # Работа в фоне: трей, фоновая проверка, автозапуск
        card, body = self._card(tr("Работа в фоне"))
        self.close_to_tray = QCheckBox(tr("При закрытии окна оставаться в трее"))
        self.close_to_tray.toggled.connect(self._emit)
        self._add_with_hint(body, self.close_to_tray, tr("Программа продолжит проверять обновления, а выйти можно "
                                                         "из меню значка возле часов."))
        self.autostart = QCheckBox(tr("Запускать вместе с Windows (сразу в трей)"))
        self.autostart.clicked.connect(self.autostart_toggled)
        self.autostart_hint = label("", "Faint", wrap=True)
        self.autostart_hint.setContentsMargins(26, 0, 0, 2)
        body.addWidget(self.autostart)
        body.addWidget(self.autostart_hint)
        row = QHBoxLayout()
        self.background_check = QCheckBox(tr("Проверять обновления модов во всех сборках каждые"))
        self.background_check.toggled.connect(self._emit)
        self.background_interval = QComboBox()
        for hours in (1, 3, 6, 12, 24):
            self.background_interval.addItem(tr("{0} ч", hours), hours)
        self.background_interval.currentIndexChanged.connect(self._emit)
        row.addWidget(self.background_check)
        row.addWidget(self.background_interval)
        row.addStretch()
        body.addLayout(row)
        row = QHBoxLayout()
        self.background_status = label("", "Muted", wrap=True)
        row.addWidget(self.background_status, 1)
        self.background_check_btn = button(tr("Проверить сейчас"), None, "refresh", tokens, "text")
        self.background_check_btn.clicked.connect(self.background_check_requested)
        row.addWidget(self.background_check_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        body.addLayout(row)
        root.addWidget(card)

        # Снимки
        card, body = self._card(tr("Снимки и бэкапы"))
        body.addWidget(label(tr("Снимок сохраняет весь набор модов, чтобы вернуться к нему со страницы «История». "
                             "Файлы связываются жёсткими ссылками и почти не занимают места."), "Muted", wrap=True))
        self.auto_snapshot = QCheckBox(tr("Снимок перед большими изменениями"))
        self.auto_snapshot.toggled.connect(self._emit)
        self._add_with_hint(body, self.auto_snapshot, tr("Перед «Обновить все», заменой сборки и сменой версии игры."))
        row = QHBoxLayout()
        row.addWidget(label(tr("Хранить автоматических снимков:")))
        self.snapshots_keep = QComboBox()
        self.snapshots_keep.addItems(["3", "5", "10", "20", "50"])
        self.snapshots_keep.currentIndexChanged.connect(self._emit)
        row.addWidget(self.snapshots_keep)
        row.addStretch()
        body.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(label(tr("Удалять бэкапы операций старше:")))
        self.backup_days = QComboBox()
        for value, title in BACKUP_DAYS:
            self.backup_days.addItem(title, value)
        self.backup_days.currentIndexChanged.connect(self._emit)
        row.addWidget(self.backup_days)
        row.addSpacing(14)
        row.addWidget(label(tr("Не больше:")))
        self.backup_mb = QComboBox()
        for value, title in BACKUP_MB:
            self.backup_mb.addItem(title, value)
        self.backup_mb.currentIndexChanged.connect(self._emit)
        row.addWidget(self.backup_mb)
        row.addStretch()
        body.addLayout(row)
        row = QHBoxLayout()
        self.backup_usage = label("", "Faint")
        row.addWidget(self.backup_usage)
        row.addStretch()
        self.prune_btn = button(tr("Очистить сейчас"), None, "trash", tokens, "text")
        self.prune_btn.clicked.connect(self.prune_backups_requested)
        row.addWidget(self.prune_btn)
        body.addLayout(row)
        root.addWidget(card)

        # Игра
        card, body = self._card(tr("Игра"))
        self.auto_crash = QCheckBox(tr("Открывать разбор, если игра вылетела"))
        self.auto_crash.toggled.connect(self._emit)
        self._add_with_hint(body, self.auto_crash, tr("Программа следит за игрой этой сборки (по занятым файлам модов и логу) "
                            "и после вылета сама показывает, какие моды упоминаются в ошибке."))
        row = QHBoxLayout()
        row.addWidget(label(tr("Автопоиск проблемного мода: ждать загрузку игры до")))
        self.bisect_timeout = QComboBox()
        for minutes in (2, 3, 5, 8, 12):
            self.bisect_timeout.addItem(tr('{0} мин', minutes), minutes)
        self.bisect_timeout.currentIndexChanged.connect(self._emit)
        row.addWidget(self.bisect_timeout)
        row.addStretch()
        body.addLayout(row)
        root.addWidget(card)

        # Лаунчеры
        card, body = self._card(tr("Лаунчеры"))
        body.addWidget(label(tr("Сборки из Prism Launcher, MultiMC, Modrinth App, CurseForge и официального "
                             "лаунчера добавляются в список автоматически."), "Muted", wrap=True))
        self.discover = QCheckBox(tr("Искать сборки при запуске"))
        self.discover.toggled.connect(self._emit)
        body.addWidget(self.discover)
        self.discover_btn = button(tr("Найти сборки сейчас"), None, "box_search", tokens, "text")
        self.discover_btn.clicked.connect(self.discover_requested)
        body.addWidget(self.discover_btn, 0, Qt.AlignmentFlag.AlignLeft)
        root.addWidget(card)

        # Данные
        card, body = self._card(tr("Данные"))
        body.addWidget(label(tr("Кэш хранит хэши файлов, информацию о модах и иконки — с ним проверка "
                             "занимает секунды. Очистка безопасна."), "Muted", wrap=True))
        row = QHBoxLayout()
        self.clear_btn = button(tr("Очистить кэш"), None, "trash", tokens, "text")
        self.clear_btn.clicked.connect(self.clear_cache_requested)
        self.data_btn = button(tr("Папка данных"), None, "folder", tokens, "text")
        self.data_btn.clicked.connect(self.open_data_requested)
        row.addWidget(self.clear_btn)
        row.addWidget(self.data_btn)
        row.addStretch()
        body.addLayout(row)
        body.addWidget(label(tr("Перенос на другой компьютер: сборки, настройки, метки, заметки, избранное и "
                             "закреплённые версии в одном файле."), "Muted", wrap=True))
        row = QHBoxLayout()
        self.export_btn = button(tr("Экспорт настроек…"), None, "share", tokens, "text")
        self.export_btn.clicked.connect(self.export_requested)
        self.import_btn = button(tr("Импорт настроек…"), None, "import", tokens, "text")
        self.import_btn.clicked.connect(self.import_requested)
        row.addWidget(self.export_btn)
        row.addWidget(self.import_btn)
        row.addStretch()
        body.addLayout(row)
        root.addWidget(card)

        # Обновление самой программы
        card, body = self._card(tr("Обновление программы"))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.app_update_status = label("", "Muted", wrap=True)
        self.app_update_status.setTextFormat(Qt.TextFormat.RichText)
        row.addWidget(self.app_update_status, 1)
        self.app_update_open_btn = button(tr("Обновить…"), "PrimaryButton", "download", tokens, "accent_text")
        self.app_update_open_btn.clicked.connect(self.app_update_open_requested)
        self.app_update_open_btn.hide()
        self.app_update_check_btn = button(tr("Проверить"), None, "refresh", tokens, "text")
        self.app_update_check_btn.clicked.connect(self.app_update_check_requested)
        row.addWidget(self.app_update_open_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.app_update_check_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        body.addLayout(row)
        self.app_update_auto = QCheckBox(tr("Проверять новые версии программы при запуске"))
        self.app_update_auto.toggled.connect(self._emit)
        self._add_with_hint(body, self.app_update_auto,
                            tr("Не чаще раза в 12 часов. Новая версия скачивается только по вашей команде."))
        root.addWidget(card)

        # Горячие клавиши
        card, body = self._card(tr("Горячие клавиши"))
        self._hotkeys_card = card
        body.addWidget(label(tr("Щёлкните по полю и нажмите новое сочетание. Крестик — убрать сочетание."), "Muted", wrap=True))
        self.shortcut_editor = ShortcutEditor()
        self.shortcut_editor.changed.connect(self._emit)
        body.addWidget(self.shortcut_editor)
        body.addWidget(label(tr("Всегда работают:"), "Muted"))
        shortcuts = label(tr("<table cellspacing='4'>"
                          "<tr><td><b>Пробел</b></td><td>&nbsp;&nbsp;отметить мод</td></tr>"
                          "<tr><td><b>Enter</b></td><td>&nbsp;&nbsp;открыть панель мода</td></tr>"
                          "<tr><td><b>→ / ←</b></td><td>&nbsp;&nbsp;раскрыть / свернуть зависимости (дерево)</td></tr>"
                          "<tr><td><b>Delete</b></td><td>&nbsp;&nbsp;удалить отмеченные</td></tr>"
                          "<tr><td><b>Esc</b></td><td>&nbsp;&nbsp;закрыть панель / сбросить поиск</td></tr>"
                          "<tr><td><b>Перетаскивание</b></td><td>&nbsp;&nbsp;.jar, .mrpack или строка сборки в окно</td></tr>"
                          "</table>"), "Muted")
        shortcuts.setTextFormat(Qt.TextFormat.RichText)
        body.addWidget(shortcuts)
        root.addWidget(card)
        root.addStretch()
        self._build_navigation(outer, scroll)

    def _build_install_card(self, tokens: dict) -> QFrame:
        """Компактная карточка: строка статуса с кнопками, параметры и описание — по кнопке «Параметры»."""
        card, body = self._card(tr("Установка программы"))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.install_status = label("", "Muted", wrap=True)
        self.install_status.setTextFormat(Qt.TextFormat.RichText)
        row.addWidget(self.install_status, 1)
        self.install_more = button(tr("Параметры"), "GhostButton", "chevron_right", tokens, "text_muted")
        self.install_more.clicked.connect(lambda: self._toggle_install_details(not self.install_details.isVisible()))
        self.install_btn = button(tr("Установить"), "PrimaryButton", "download", tokens, "accent_text")
        self.install_btn.clicked.connect(lambda: self.install_requested.emit({
            "desktop": self.install_desktop.isChecked(), "associate_mrpack": self.install_mrpack.isChecked(),
            "protocol": self.install_protocol.isChecked()}))
        self.uninstall_btn = button(tr("Удалить программу…"), "DangerButton", "trash", tokens, "danger")
        self.uninstall_btn.clicked.connect(self.uninstall_requested)
        for widget in (self.install_more, self.install_btn, self.uninstall_btn):
            row.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
        body.addLayout(row)

        self.install_details = QWidget()
        details = QVBoxLayout(self.install_details)
        details.setContentsMargins(0, 4, 0, 0)
        details.setSpacing(8)
        self.install_desktop = QCheckBox(tr("Ярлык на рабочем столе"))
        self.install_mrpack = QCheckBox(tr("Открывать файлы .mrpack двойным щелчком"))
        self.install_protocol = QCheckBox(tr("Открывать ссылки modupdater:// (кнопки «Открыть в Mod Updater» на сайтах)"))
        for box in (self.install_desktop, self.install_mrpack, self.install_protocol):
            box.setChecked(True)
            details.addWidget(box)
        self.install_info = label("", "Faint", wrap=True)
        self.install_info.setTextFormat(Qt.TextFormat.RichText)
        details.addWidget(self.install_info)
        self.install_details.hide()
        self._apply_only_with_details = False
        body.addWidget(self.install_details)
        return card

    def _toggle_install_details(self, visible: bool):
        self.install_details.setVisible(visible)
        if self._apply_only_with_details:  # «Применить» нужна, только когда видны галочки
            self.install_btn.setVisible(visible)
        name = "chevron_down" if visible else "chevron_right"
        self.install_more.setProperty("icon_name", name)
        self.install_more.setIcon(icons.icon(name, self.tokens["text_muted"], 16))

    def _place_install_card(self, top: bool):
        """Не установлена — карточка вверху (это действие), установлена — внизу, рядом с «Данными»."""
        self._root.removeWidget(self.install_card)
        index = 1 if top else self._root.indexOf(self._hotkeys_card)
        self._root.insertWidget(index, self.install_card)
        self._fill_navigation()

    @staticmethod
    def _add_with_hint(body, widget, hint: str = None):
        body.addWidget(widget)
        if hint:
            hint_label = label(hint, "Faint", wrap=True)
            hint_label.setContentsMargins(26, 0, 0, 2)
            body.addWidget(hint_label)

    def _card(self, title: str):
        card = QFrame()
        card.setObjectName("Card")
        body = QVBoxLayout(card)
        body.setContentsMargins(18, 16, 18, 16)
        body.setSpacing(10)
        body.addWidget(label(title, "SectionTitle"))
        self._sections.append((title, card))
        return card, body

    # --- Навигация и поиск по разделам ---

    def _build_navigation(self, outer: QHBoxLayout, scroll: QScrollArea):
        """Колонка слева: поиск по настройкам и список разделов (щелчок — прокрутить к разделу)."""
        self._scroll = scroll
        self.nav = QWidget()
        self.nav.setFixedWidth(230)
        lay = QVBoxLayout(self.nav)
        lay.setContentsMargins(24, 30, 4, 20)
        lay.setSpacing(10)
        self.settings_search = SearchBox(self.tokens, tr("Найти настройку…"))
        self.settings_search.setMinimumHeight(34)
        self.settings_search.textChanged.connect(self._filter_sections)
        lay.addWidget(self.settings_search)
        self.nav_list = QListWidget()
        self.nav_list.setObjectName("SettingsNav")
        self.nav_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav_list.itemClicked.connect(lambda item: self._scroll_to(item.data(Qt.ItemDataRole.UserRole)))
        lay.addWidget(self.nav_list, 1)
        self.search_status = label("", "Faint", wrap=True)
        self.search_status.hide()
        lay.addWidget(self.search_status)
        outer.insertWidget(0, self.nav)
        self._fill_navigation()
        scroll.verticalScrollBar().valueChanged.connect(self._sync_navigation)

    def _fill_navigation(self):
        """Разделы в порядке карточек на странице (карточка установки может переезжать)."""
        if not hasattr(self, "nav_list"):
            return
        self.nav_list.clear()
        for title, card in sorted(self._sections, key=lambda s: self._root.indexOf(s[1])):
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, card)
            item.setHidden(card.isHidden())
            self.nav_list.addItem(item)

    def _scroll_to(self, card: QWidget):
        top = card.mapTo(self._scroll.widget(), card.rect().topLeft()).y()
        self._scroll.verticalScrollBar().setValue(max(0, top - 16))

    def _sync_navigation(self, value: int):
        """Подсветка раздела, который сейчас наверху страницы."""
        current = None
        for i in range(self.nav_list.count()):
            item = self.nav_list.item(i)
            card = item.data(Qt.ItemDataRole.UserRole)
            if item.isHidden():
                continue
            if card.mapTo(self._scroll.widget(), card.rect().topLeft()).y() <= value + 80 or current is None:
                current = item
        if current is not None and current is not self.nav_list.currentItem():
            self.nav_list.blockSignals(True)
            self.nav_list.setCurrentItem(current)
            self.nav_list.blockSignals(False)

    @staticmethod
    def _section_text(card: QWidget) -> str:
        """Всё, что написано в карточке: заголовки, подписи, галочки, кнопки, пункты списков."""
        import re
        parts = [w.text() for w in card.findChildren(QLabel)]
        parts += [w.text() for w in card.findChildren(QAbstractButton)]
        for combo in card.findChildren(QComboBox):
            parts += [combo.itemText(i) for i in range(combo.count())]
        return re.sub(r"<[^>]+>", " ", " ".join(parts)).lower()

    def _filter_sections(self, text: str):
        query = text.strip().lower()
        shown = 0
        for i in range(self.nav_list.count()):
            item = self.nav_list.item(i)
            card = item.data(Qt.ItemDataRole.UserRole)
            visible = not query or all(word in self._section_text(card) for word in query.split())
            card.setVisible(visible)
            item.setHidden(not visible)
            shown += visible
        self.search_status.setVisible(bool(query))
        self.search_status.setText(tr("Ничего не найдено") if not shown else tr("Разделов: {0}", shown))
        self._scroll.verticalScrollBar().setValue(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "nav"):
            self.nav.setVisible(self.width() >= 860)  # В узком окне колонка разделов прячется

    def set_tokens(self, tokens: dict):
        self.tokens = tokens
        refresh_button_icons(self, tokens)
        if hasattr(self, "settings_search"):
            self.settings_search.set_tokens(tokens)

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
        self._accent = settings.get("accent") or DEFAULT_ACCENT
        for b in self.accent_group.buttons():
            b.setChecked(b.property("accent_key") == self._accent)
        self.custom_accent.setText(tr("Свой цвет ✓") if self._accent.startswith("#") else tr("Свой цвет…"))
        self.scale.setCurrentIndex(max(0, self.scale.findData(int(settings.get("ui_scale", 100)))))
        self.language.setCurrentIndex(max(0, self.language.findData(settings.get("language") or language())))
        self.minimal.setChecked(bool(settings.get("minimal_mode", False)))
        self.app_update_auto.setChecked(bool(settings.get("app_update_check", True)))
        self.min_age.setCurrentIndex(max(0, self.min_age.findData(int(settings.get("update_min_age_days", 0) or 0))))
        self.close_to_tray.setChecked(bool(settings.get("close_to_tray", False)))
        self.background_check.setChecked(bool(settings.get("background_check", True)))
        self.background_interval.setCurrentIndex(
            max(0, self.background_interval.findData(int(settings.get("background_interval_hours", 6)))))
        self.auto_crash.setChecked(bool(settings.get("auto_crash_analysis", True)))
        self.bisect_timeout.setCurrentIndex(max(0, self.bisect_timeout.findData(int(settings.get("auto_bisect_timeout", 5)))))
        self._sidebar_image = settings.get("sidebar_image") or ""
        self.sidebar_image_clear.setVisible(bool(self._sidebar_image))
        self.per_profile.setChecked(bool(settings.get("per_profile_view", True)))
        self.abandoned.setChecked(bool(settings.get("abandoned_warning", True)))
        columns = set(settings.get("columns") or ())
        for key, box in self.column_boxes.items():
            box.setChecked(key in columns)
        self.backup_days.setCurrentIndex(max(0, self.backup_days.findData(int(settings.get("backup_max_days", 30)))))
        self.backup_mb.setCurrentIndex(max(0, self.backup_mb.findData(int(settings.get("backup_max_mb", 1024)))))
        self.shortcut_editor.load(settings.get("shortcuts") or {})
        self._loading = False

    def set_install_state(self, supported: bool, state=None):
        """Раздел «Установка»: одна строка состояния; что даёт установка — в «Параметрах»."""
        self.install_info.setText(tr("<b>Что даёт установка:</b><br>"
                                     "• ярлыки в меню «Пуск» и на рабочем столе;<br>"
                                     "• пункт в «Установке и удалении программ» Windows;<br>"
                                     "• файлы .mrpack открываются двойным щелчком — сразу в режиме импорта;<br>"
                                     "• ссылки modupdater:// — сборки и моды открываются из браузера и Discord;<br>"
                                     "• второй запуск не открывает второе окно, а передаёт файл уже открытой программе.<br><br>"
                                     "<b>Что появится в следующих версиях:</b><br>"
                                     "• кнопки «Установить в Mod Updater» по ссылкам с сайтов и из сборок друзей."))
        boxes = (self.install_desktop, self.install_mrpack, self.install_protocol)
        installed = bool(supported and state and state.installed)
        self._place_install_card(top=supported and not installed)
        if not supported:
            self.install_status.setText(tr("Доступна в собранной программе (.exe) для Windows."))
            for widget in (*boxes, self.install_btn):
                widget.setEnabled(False)
            self.uninstall_btn.hide()
            return
        for widget in (*boxes, self.install_btn):
            widget.setEnabled(True)
        if installed:
            where = tr("эта копия") if state.running_installed else tr('версия {0}', state.installed_version)
            self.install_status.setText(tr("<span style='color:{0}'><b>✓ Установлена</b></span> · {1}",
                                           self.tokens["success"], where))
            self.install_status.setToolTip(state.exe)
            self.install_btn.setText(tr("Применить") if state.running_installed else tr("Обновить"))
            self._apply_only_with_details = bool(state.running_installed)
            self.install_btn.setVisible(not state.running_installed or self.install_details.isVisible())
            self.install_desktop.setChecked(state.desktop)
            self.install_mrpack.setChecked(state.mrpack)
            self.install_protocol.setChecked(state.protocol)
            self.uninstall_btn.show()
        else:
            self.install_status.setText(tr("Ярлыки, .mrpack двойным щелчком и ссылки modupdater:// — "
                                           "пара секунд, без прав администратора."))
            self.install_status.setToolTip("")
            self._apply_only_with_details = False
            self.install_btn.setText(tr("Установить"))
            self.install_btn.show()
            self.uninstall_btn.hide()

    def set_background_state(self, autostart: bool, autostart_supported: bool, tray_available: bool,
                             last_check, updates: int):
        """Состояние карточки «Работа в фоне»: автозапуск (только для .exe), трей, последняя проверка."""
        import datetime
        self.autostart.setChecked(autostart)
        self.autostart.setEnabled(autostart_supported)
        self.autostart_hint.setText("" if autostart_supported else
                                    tr("Доступно в собранной программе (.exe) для Windows."))
        self.autostart_hint.setVisible(not autostart_supported)
        self.close_to_tray.setEnabled(tray_available)
        if not last_check:
            text = tr("Фоновая проверка ещё не запускалась.")
        else:
            when = datetime.datetime.fromtimestamp(float(last_check)).strftime("%H:%M")
            text = tr("Последняя проверка в {0}: обновлений {1}.", when, updates) if updates else \
                tr("Последняя проверка в {0}: все моды актуальны.", when)
        self.background_status.setText(text)

    def set_app_update_state(self, text: str, available: bool = False, checking: bool = False):
        """Строка состояния обновления программы; «Обновить…» — когда есть новая версия."""
        self.app_update_status.setText(text)
        self.app_update_open_btn.setVisible(available)
        self.app_update_check_btn.setEnabled(not checking)

    def set_backup_usage(self, text: str):
        self.backup_usage.setText(text)

    def _pick_sidebar_image(self):
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(self, tr("Картинка для боковой панели"), "",
                                              tr("Картинки (*.png *.jpg *.jpeg *.webp *.bmp)"))
        if path:
            self._set_sidebar_image(path)

    def _set_sidebar_image(self, path: str):
        self._sidebar_image = path
        self.sidebar_image_clear.setVisible(bool(path))
        self._emit()

    def _set_accent(self, accent: str):
        self._accent = accent
        self.custom_accent.setText(tr("Свой цвет ✓") if accent.startswith("#") else tr("Свой цвет…"))
        self._emit()

    def _pick_accent(self):
        start = QColor(self._accent) if self._accent.startswith("#") else QColor(ACCENTS[DEFAULT_ACCENT][1])
        color = QColorDialog.getColor(start, self, tr("Акцентный цвет"))
        if color.isValid():
            for b in self.accent_group.buttons():
                b.setChecked(False)
            self._set_accent(color.name())

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
                "accent": self._accent,
                "ui_scale": self.scale.currentData(),
                "language": self.language.currentData(),
                "minimal_mode": self.minimal.isChecked(),
                "app_update_check": self.app_update_auto.isChecked(),
                "update_min_age_days": self.min_age.currentData(),
                "close_to_tray": self.close_to_tray.isChecked(),
                "background_check": self.background_check.isChecked(),
                "background_interval_hours": self.background_interval.currentData(),
                "auto_crash_analysis": self.auto_crash.isChecked(),
                "auto_bisect_timeout": self.bisect_timeout.currentData(),
                "sidebar_image": self._sidebar_image,
                "per_profile_view": self.per_profile.isChecked(),
                "abandoned_warning": self.abandoned.isChecked(),
                "columns": [key for key, box in self.column_boxes.items() if box.isChecked()],
                "backup_max_days": self.backup_days.currentData(),
                "backup_max_mb": self.backup_mb.currentData(),
                "shortcuts": dict(self.shortcut_editor._overrides),
            })
