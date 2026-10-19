"""Главное окно приложения."""
import datetime
import logging
import os
import re
import sys
import webbrowser
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import QObject, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPalette
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QStyle,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import config as app_config
from constants import DARK_STYLE, LIGHT_STYLE, THEME_COLORS, TableColumn
from enums import INSTALLABLE_STATUSES, ModStatus, UpdateChannel
from exporters import (
    build_mrpack_index, build_share_data, decode_share_string, encode_share_string, parse_share_data,
    save_server_json, write_mrpack,
)
from game_detection import detect_game_versions, extract_loader_version
from mod_updater_logic import get_latest_loader_version, select_primary_file, format_date
from ui_components import (
    ImportDialog, ShareDialog, SyncDialog, TitleBar, VersionPickerDialog, ask_choice, confirm, show_message,
)
from updater import (
    UpdateTransaction, describe_transaction, find_locked_files, get_last_transaction, is_game_running,
    move_all_to_backup, remove_mods, rollback_transaction,
)
from workers import (
    IconLoader, ImportWorker, LoaderVersionWorker, LocalScanWorker, ScanWorker, TaskWorker, UpdateWorker,
    fetch_json, fetch_versions,
)

LOADERS = ["fabric", "forge", "neoforge", "quilt"]
# Статусы, которые попадают в «Обновить все» (включая недостающие обязательные зависимости)
BULK_UPDATE_STATUSES = INSTALLABLE_STATUSES + (ModStatus.REQUIRED,)


# --- Логирование в GUI ---

class LogEmitter(QObject):
    log_signal = pyqtSignal(str)


class QtLogHandler(logging.Handler):
    """Перенаправляет логи в GUI через сигнал (потокобезопасно)."""

    def __init__(self, emitter: LogEmitter):
        super().__init__(level=logging.INFO)
        self.emitter = emitter

    def emit(self, record):
        try:
            self.emitter.log_signal.emit(self.format(record))
        except RuntimeError:
            pass  # Окно уже уничтожено


# --- Кэш профилей ---

class ProfileSession:
    """Данные таблицы конкретного профиля в памяти."""

    def __init__(self, mods_data=None, is_import_mode=False):
        self.mods_data = mods_data if mods_data is not None else []
        self.is_import_mode = is_import_mode
        self.last_scanned = datetime.datetime.now()


class ModUpdaterApp(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Minecraft Mod Updater")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setGeometry(100, 100, 1280, 720)
        self.setMinimumSize(800, 600)

        self.mods_path = ""
        self.cache_file = app_config.CACHE_FILE
        self.scan_worker: Optional[ScanWorker] = None
        self.import_worker: Optional[ImportWorker] = None
        self.local_scan_worker: Optional[LocalScanWorker] = None
        self.update_worker: Optional[UpdateWorker] = None
        self.loader_version_worker: Optional[LoaderVersionWorker] = None
        self._task_workers: List[TaskWorker] = []

        self.last_loader_status: Optional[tuple[str, str]] = None
        self.current_loader_version: Optional[str] = None
        self.latest_loader_version: Optional[str] = None

        self.mods_data: list = []
        self.row_to_dependencies_map: Dict[int, List[int]] = {}
        self.mod_index_to_rows_map: Dict[int, int] = {}
        self.row_to_data_map: Dict[int, dict] = {}
        self.project_rows_map: Dict[str, List[int]] = {}
        self.is_import_mode = False
        self.all_dependencies_expanded = False
        self.current_sort_col = -1
        self.current_sort_order = Qt.SortOrder.AscendingOrder

        self.profiles: Dict[str, dict] = {}
        self.current_profile_name = ""
        self.profile_sessions: Dict[str, ProfileSession] = {}
        self._is_switching_profile = False
        self._header_widgets_positioned = False

        self.config_theme = 'system'
        self.active_theme = 'light'

        self.log_emitter = LogEmitter()
        self.icon_loader = IconLoader(app_config.ICON_CACHE_DIR, self)
        self.icon_loader.icon_ready.connect(self._on_icon_ready)
        self._icons: Dict[str, QIcon] = {}

        # Отложенные действия: фильтр при вводе текста и проверка пути после окончания ввода
        self._filter_timer = QTimer(self, singleShot=True, interval=200)
        self._filter_timer.timeout.connect(self.filter_table)
        self._path_timer = QTimer(self, singleShot=True, interval=500)
        self._path_timer.timeout.connect(lambda: self.check_mods_folder(self.path_input.text()))

        self._build_ui()
        self.setup_gui_logging()
        self.load_config()
        self.apply_theme()

    # ------------------------------------------------------------------ UI

    def _std_icon(self, pixmap: QStyle.StandardPixmap) -> QIcon:
        return self.style().standardIcon(pixmap)

    def _build_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.title_bar = TitleBar(self)
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        layout.addWidget(self.title_bar)
        layout.addWidget(content_widget)

        content_layout.addLayout(self._build_top_panel())
        content_layout.addWidget(self._build_settings_panel())
        content_layout.addLayout(self._build_search_panel())
        content_layout.addLayout(self._build_table_actions_panel())
        self._build_table()
        content_layout.addWidget(self.table)
        content_layout.addLayout(self._build_bottom_panel())

        self.log_console = QPlainTextEdit()
        self.log_console.setReadOnly(True)
        self.log_console.setMaximumBlockCount(5000)  # Не даём логу расти бесконечно
        self.log_console.setVisible(False)
        self.toggle_log_btn = QPushButton("Показать/скрыть лог")
        self.toggle_log_btn.setIcon(self._std_icon(QStyle.StandardPixmap.SP_MessageBoxInformation))
        self.toggle_log_btn.setCheckable(True)
        self.toggle_log_btn.toggled.connect(self.log_console.setVisible)
        content_layout.addWidget(self.toggle_log_btn)
        content_layout.addWidget(self.log_console)

        self.progress_label = QLabel("")
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumWidth(300)
        self.progress_bar.setTextVisible(False)
        self.progress_label.hide()
        self.progress_bar.hide()
        self.statusBar().addPermanentWidget(self.progress_label)
        self.statusBar().addPermanentWidget(self.progress_bar)
        self.statusBar().setSizeGripEnabled(True)

    def _build_top_panel(self) -> QHBoxLayout:
        top_panel = QHBoxLayout()
        top_panel.addWidget(QLabel("Профиль:"))
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(150)
        self.profile_combo.currentIndexChanged.connect(self.on_profile_changed)
        top_panel.addWidget(self.profile_combo)

        for text, tooltip, slot in (("➕", "Добавить профиль", self.add_profile),
                                    ("✏️", "Переименовать профиль", self.rename_profile),
                                    ("🗑️", "Удалить профиль", self.delete_profile)):
            btn = QPushButton(text)
            btn.setToolTip(tooltip)
            btn.clicked.connect(slot)
            top_panel.addWidget(btn)
        top_panel.addSpacing(15)

        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("Выберите папку с игрой (где находится папка mods)")
        self.path_input.textChanged.connect(self._on_path_text_changed)
        self.path_input.editingFinished.connect(self._flush_path_timer)
        self.browse_btn = QPushButton("Обзор...")
        self.browse_btn.setIcon(self._std_icon(QStyle.StandardPixmap.SP_DirIcon))
        self.browse_btn.clicked.connect(self.browse_folder)
        self.open_mods_folder_btn = QPushButton("Открыть папку mods")
        self.open_mods_folder_btn.setIcon(self._std_icon(QStyle.StandardPixmap.SP_DirOpenIcon))
        self.open_mods_folder_btn.clicked.connect(self.open_mods_folder)
        self.status_label = QLabel("Папка mods не найдена")

        top_panel.addWidget(self.path_input, 1)
        top_panel.addWidget(self.browse_btn)
        top_panel.addWidget(self.open_mods_folder_btn)
        top_panel.addWidget(self.status_label)
        top_panel.addSpacing(15)

        self.import_export_btn = QPushButton("Управление сборкой ▼")
        menu = QMenu(self)
        SP = QStyle.StandardPixmap
        self.action_import = QAction(self._std_icon(SP.SP_ArrowDown), "Импортировать список", self)
        self.action_sync = QAction(self._std_icon(SP.SP_BrowserReload), "Синхронизация по URL", self)
        self.action_export_json = QAction(self._std_icon(SP.SP_DialogSaveButton), "Экспорт JSON", self)
        self.action_share = QAction(self._std_icon(SP.SP_ArrowUp), "Поделиться списком", self)
        self.action_export_mrpack = QAction(self._std_icon(SP.SP_DialogSaveButton), "Экспорт в .mrpack", self)
        self.action_export_mrpack.setToolTip("Экспорт сборки для Modrinth App или Prism Launcher")
        self.action_rollback = QAction(self._std_icon(SP.SP_ArrowBack), "Откатить последнее изменение...", self)
        self.action_rollback.setToolTip("Вернуть файлы модов в состояние до последнего обновления, установки или удаления")

        self.action_import.triggered.connect(self.import_mod_list)
        self.action_sync.triggered.connect(self.sync_from_url)
        self.action_export_json.triggered.connect(self.export_server_json)
        self.action_share.triggered.connect(self.share_mod_list)
        self.action_export_mrpack.triggered.connect(self.export_mrpack)
        self.action_rollback.triggered.connect(self.rollback_last_change)

        for action in (self.action_import, self.action_sync, None, self.action_export_json, self.action_share,
                       self.action_export_mrpack, None, self.action_rollback):
            menu.addSeparator() if action is None else menu.addAction(action)
        self.import_export_btn.setMenu(menu)
        top_panel.addWidget(self.import_export_btn)
        return top_panel

    def _build_settings_panel(self) -> QGroupBox:
        settings_group = QGroupBox("Настройки сканирования")
        panel = QHBoxLayout(settings_group)
        panel.addWidget(QLabel("Версия игры:"))
        self.game_version_combo = QComboBox()
        self.game_version_combo.setEditable(True)
        panel.addWidget(self.game_version_combo, 1)

        panel.addWidget(QLabel("Загрузчик:"))
        self.loader_combo = QComboBox()
        self.loader_combo.addItems(LOADERS)
        panel.addWidget(self.loader_combo, 1)

        self.loader_status_label = QLabel("")
        self.loader_status_label.setToolTip("Статус версии загрузчика (проверяется автоматически)")
        panel.addWidget(self.loader_status_label)

        panel.addWidget(QLabel("Канал:"))
        self.channel_combo = QComboBox()
        for channel in UpdateChannel:
            self.channel_combo.addItem(channel.display_name, channel.value)
        self.channel_combo.setToolTip("Какие версии модов предлагать для обновления")
        self.channel_combo.currentIndexChanged.connect(self._on_channel_changed)
        panel.addWidget(self.channel_combo, 1)

        self.manual_mode_checkbox = QCheckBox("Ручной режим")
        self.manual_mode_checkbox.toggled.connect(self.on_manual_mode_toggled)
        panel.addWidget(self.manual_mode_checkbox)
        panel.addStretch(3)

        self.game_version_combo.setEnabled(False)
        self.loader_combo.setEnabled(False)
        return settings_group

    def _build_search_panel(self) -> QHBoxLayout:
        layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Поиск по названию мода...")
        self.search_input.textChanged.connect(lambda: self._filter_timer.start())
        self.view_mode_combo = QComboBox()
        self.view_mode_combo.addItems(["Вид: Иерархия (С зависимостями)", "Вид: Простой список (Мои моды)",
                                       "Фильтр: Не найдены на Modrinth", "Фильтр: Нуждаются в обновлении"])
        self.view_mode_combo.currentIndexChanged.connect(self.filter_table)
        layout.addWidget(self.search_input)
        layout.addWidget(self.view_mode_combo)
        return layout

    def _build_table_actions_panel(self) -> QHBoxLayout:
        layout = QHBoxLayout()
        self.invert_selection_btn = QPushButton("Инвертировать выделение")
        self.invert_selection_btn.setToolTip("Снять выделение с выбранных и выделить невыбранные")
        self.invert_selection_btn.clicked.connect(self.invert_selection)
        self.delete_selected_btn = QPushButton("Удалить выбранные")
        self.delete_selected_btn.setIcon(self._std_icon(QStyle.StandardPixmap.SP_TrashIcon))
        self.delete_selected_btn.clicked.connect(self.delete_selected_mods)
        self.refresh_list_btn = QPushButton("Обновить список (Force Refresh)")
        self.refresh_list_btn.setIcon(self._std_icon(QStyle.StandardPixmap.SP_BrowserReload))
        self.refresh_list_btn.setToolTip("Принудительно пересканировать папку (игнорируя кэш профиля)")
        self.refresh_list_btn.clicked.connect(self.force_refresh_list)
        for btn in (self.invert_selection_btn, self.delete_selected_btn, self.refresh_list_btn):
            btn.setEnabled(False)
        layout.addWidget(self.invert_selection_btn)
        layout.addWidget(self.delete_selected_btn)
        layout.addStretch()
        layout.addWidget(self.refresh_list_btn)
        return layout

    def _build_table(self):
        self.table = QTableWidget()
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setColumnCount(len(TableColumn))
        self.table.setHorizontalHeaderLabels(["", "Название мода", "Текущая версия", "Новая версия",
                                              "Дата выхода", "Статус", "Действие"])
        self.table.setIconSize(QSize(20, 20))
        header = self.table.horizontalHeader()
        for column in TableColumn:
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        for column, width in ((TableColumn.CHECKBOX, 60), (TableColumn.NAME, 300), (TableColumn.CURRENT_VERSION, 110),
                              (TableColumn.NEW_VERSION, 110), (TableColumn.RELEASE_DATE, 90),
                              (TableColumn.STATUS, 150), (TableColumn.ACTION, 140)):
            self.table.setColumnWidth(column, width)
        header.setStretchLastSection(True)
        header.sectionResized.connect(self.on_section_resized)
        header.sectionClicked.connect(self.on_header_clicked)
        header.setSortIndicatorShown(True)

        self.master_checkbox = QCheckBox(header)
        self.master_checkbox.setTristate(True)
        self.master_checkbox.hide()
        self.master_checkbox.stateChanged.connect(self.on_master_checkbox_changed)

        self.toggle_deps_btn = QPushButton(header)
        self.toggle_deps_btn.setIcon(self._std_icon(QStyle.StandardPixmap.SP_ArrowDown))
        self.toggle_deps_btn.setToolTip("Раскрыть все зависимости")
        self.toggle_deps_btn.setFixedSize(22, 22)
        self.toggle_deps_btn.clicked.connect(self.toggle_all_dependencies)
        self.toggle_deps_btn.hide()

    def _build_bottom_panel(self) -> QHBoxLayout:
        panel = QHBoxLayout()
        SP = QStyle.StandardPixmap
        self.scan_btn = QPushButton("Сканировать")
        self.scan_btn.setIcon(self._std_icon(SP.SP_BrowserReload))
        self.scan_btn.clicked.connect(self.scan_mods)

        self.update_selected_btn = QPushButton("Обновить выбранные")
        self.update_unselected_btn = QPushButton("Обновить всё, кроме выбранных")
        self.update_all_btn = QPushButton("Обновить все")
        self.install_selected_btn = QPushButton("Установить выбранные")
        self.install_unselected_btn = QPushButton("Установить всё, кроме выбранных")
        self.install_all_btn = QPushButton("Установить все")
        self.update_buttons = (self.update_selected_btn, self.update_unselected_btn, self.update_all_btn)
        self.install_buttons = (self.install_selected_btn, self.install_unselected_btn, self.install_all_btn)

        for buttons in (self.update_buttons, self.install_buttons):
            selected, unselected, all_btn = buttons
            selected.setIcon(self._std_icon(SP.SP_ArrowDown))
            unselected.setIcon(self._std_icon(SP.SP_ArrowUp))
            all_btn.setIcon(self._std_icon(SP.SP_MediaSeekForward))
            selected.clicked.connect(self.update_selected)
            unselected.clicked.connect(self.update_unselected)
            all_btn.clicked.connect(self.update_all)

        panel.addWidget(self.scan_btn)
        for btn in self.update_buttons + self.install_buttons:
            btn.setEnabled(False)
            panel.addWidget(btn)
        for btn in self.install_buttons:
            btn.hide()
        self.scan_btn.setEnabled(False)
        return panel

    def setup_gui_logging(self):
        handler = QtLogHandler(self.log_emitter)
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logging.getLogger().addHandler(handler)
        self._gui_log_handler = handler
        self.log_emitter.log_signal.connect(self.log_console.appendPlainText)

    # ------------------------------------------------------------------ События окна

    def keyPressEvent(self, event):
        """Исправляет Alt+F4 для окна без рамки."""
        if event.key() == Qt.Key.Key_F4 and event.modifiers() == Qt.KeyboardModifier.AltModifier:
            self.close()
            event.accept()
        else:
            super().keyPressEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        # Позиционируем виджеты в заголовке, когда размеры уже известны
        if not self._header_widgets_positioned:
            self.reposition_header_widgets()
            self._header_widgets_positioned = True

    def changeEvent(self, event):
        if event.type() == event.Type.WindowStateChange:
            self.title_bar.maximize_button.setText("🗗" if self.isMaximized() else "🗖")
        super().changeEvent(event)

    def closeEvent(self, event):
        workers = [w for w in (self.scan_worker, self.import_worker, self.local_scan_worker, self.update_worker,
                               self.loader_version_worker, *self._task_workers) if w and w.isRunning()]
        if workers:
            logging.info("Приложение закрывается, останавливаем фоновые задачи...")
            for worker in workers:
                if hasattr(worker, 'stop'):
                    worker.stop()
            for worker in workers:
                worker.wait(10000)
        self.icon_loader.shutdown()
        self.save_config()
        logging.getLogger().removeHandler(self._gui_log_handler)
        super().closeEvent(event)

    def reposition_header_widgets(self):
        header_height = self.table.horizontalHeader().height()
        start_x = 6
        cb_width = self.master_checkbox.sizeHint().width()
        self.master_checkbox.move(start_x, (header_height - self.master_checkbox.sizeHint().height()) // 2)
        self.toggle_deps_btn.move(start_x + cb_width + 4, (header_height - self.toggle_deps_btn.height()) // 2)

    def on_section_resized(self, logical_index, old_size, new_size):
        if logical_index == TableColumn.CHECKBOX:
            self.reposition_header_widgets()

    # ------------------------------------------------------------------ Тема

    def get_windows_theme(self) -> Optional[str]:
        if sys.platform != "win32":
            return None
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as key:
                value, _ = winreg.QueryValueEx(key, 'AppsUseLightTheme')
            return 'light' if value else 'dark'
        except OSError as e:
            logging.warning(f"Не удалось прочитать тему Windows из реестра ({e}).")
            return None

    def toggle_theme(self):
        themes = ['system', 'light', 'dark']
        index = themes.index(self.config_theme) if self.config_theme in themes else 0
        self.config_theme = themes[(index + 1) % len(themes)]
        logging.info(f"Тема оформления переключена на: {self.config_theme}")
        self.apply_theme()
        if self.mods_data:
            self._sync_table_to_mods_data()
            self.populate_table()  # Цвета строк зависят от темы

    def apply_theme(self):
        theme_names = {'system': 'Системная', 'light': 'Светлая', 'dark': 'Тёмная'}
        self.title_bar.toggle_theme_btn.setText(f"Тема: {theme_names.get(self.config_theme, 'Системная')}")

        if self.config_theme == 'system':
            windows_theme = self.get_windows_theme()
            if windows_theme:
                self.active_theme = windows_theme
            else:
                is_dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
                self.active_theme = 'dark' if is_dark else 'light'
        else:
            self.active_theme = self.config_theme

        self.setStyleSheet(DARK_STYLE if self.active_theme == 'dark' else LIGHT_STYLE)
        status_key = 'status_ok' if self.mods_path else 'status_error'
        self.status_label.setStyleSheet(f"color: {self._color(status_key)};")
        if self.last_loader_status:
            self._show_loader_status(*self.last_loader_status)

        border = self._color('table_border')
        self.table.setStyleSheet(f"""
            QHeaderView::section:horizontal {{ border-bottom: 1px solid {border}; }}
            QTableCornerButton::section {{ border-bottom: 1px solid {border}; }}
        """)

    def _color(self, key: str) -> str:
        return THEME_COLORS[self.active_theme][key]

    # ------------------------------------------------------------------ Папка игры и версии

    def _on_path_text_changed(self, _text):
        if not self._is_switching_profile:
            self._path_timer.start()

    def _flush_path_timer(self):
        if self._path_timer.isActive():
            self._path_timer.stop()
            self.check_mods_folder(self.path_input.text())

    def _set_path(self, path: str):
        """Устанавливает путь и сразу проверяет папку (без задержки ввода)."""
        self.path_input.blockSignals(True)
        self.path_input.setText(path)
        self.path_input.blockSignals(False)
        self._path_timer.stop()
        self.check_mods_folder(path)

    def browse_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Выберите папку с игрой (где находится папка mods)",
                                                self.path_input.text())
        if path:
            self._set_path(path)

    def open_mods_folder(self):
        if self.mods_path and os.path.isdir(self.mods_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.mods_path))
        else:
            show_message(self, QMessageBox.Icon.Warning, "Ошибка",
                         "Папка mods не найдена. Пожалуйста, укажите правильный путь к игре.")

    def check_mods_folder(self, path: str):
        """Проверяет наличие папки mods и загружает список модов (из кэша профиля или локальным сканом)."""
        if self._is_busy():
            return
        mods_path = os.path.join(path, "mods") if path else ""
        if not mods_path or not os.path.isdir(mods_path):
            self.mods_path = ""
            self.status_label.setText("Папка mods не найдена")
            self.status_label.setStyleSheet(f"color: {self._color('status_error')};")
            self.mods_data = []
            self.populate_table()
            self.set_buttons_enabled(True)
            return

        self.mods_path = mods_path
        self.status_label.setText("Папка mods найдена!")
        self.status_label.setStyleSheet(f"color: {self._color('status_ok')};")
        self.detect_game_versions()

        session = self.profile_sessions.get(self.current_profile_name) if self._is_switching_profile else None
        if session:
            logging.info(f"Загрузка списка модов из памяти для профиля {self.current_profile_name} "
                         f"(сканировался: {session.last_scanned:%H:%M:%S})")
            self.mods_data = session.mods_data
            self.is_import_mode = session.is_import_mode
            self.populate_table()
            self.set_buttons_enabled(True)
        else:
            self.start_local_scan()

    def detect_game_versions(self):
        """Определяет версии игры и загрузчики по папке versions и запускает проверку версии загрузчика."""
        game_dir = self.path_input.text()
        result = detect_game_versions(game_dir)
        self.loader_status_label.setText("")
        self.last_loader_status = None
        self.current_loader_version = None
        self.latest_loader_version = None
        if not result.versions:
            logging.warning("Не удалось автоматически определить установленные версии.")
            return
        logging.info(f"Обнаружены версии: {result.game_versions} и загрузчики: {result.loaders}")

        # В ручном режиме выбор пользователя не перезаписываем
        if not self.manual_mode_checkbox.isChecked():
            for combo, items in ((self.game_version_combo, result.game_versions), (self.loader_combo, result.loaders)):
                combo.blockSignals(True)
                combo.clear()
                combo.addItems(items)
                combo.blockSignals(False)
            if result.best_guess:
                self.game_version_combo.setCurrentText(result.best_guess[0])
                self.loader_combo.setCurrentText(result.best_guess[1])

        if not result.best_guess or result.best_guess != (self.game_version_combo.currentText(), self.loader_combo.currentText()):
            return
        game_version, loader = result.best_guess
        self.current_loader_version = extract_loader_version(loader, result.best_guess_id or "")
        if self.current_loader_version:
            logging.info(f"Определена текущая версия загрузчика: {self.current_loader_version}")
            self.loader_status_label.setText("🔄 Проверка...")
            self.loader_status_label.setStyleSheet("")
            worker = LoaderVersionWorker(loader, game_version, self.current_loader_version)
            worker.finished.connect(self.on_loader_version_check_finished)
            self.loader_version_worker = worker
            worker.start()

    def on_loader_version_check_finished(self, message, color_key, latest_version):
        if self.sender() is not self.loader_version_worker:
            return  # Результат устаревшей проверки
        self.latest_loader_version = latest_version or None
        self._show_loader_status(message, color_key)

    def _show_loader_status(self, message, color_key):
        self.last_loader_status = (message, color_key)
        self.loader_status_label.setText(message)
        colors = {'ok': (self._color('status_ok'), "Версия загрузчика актуальна."),
                  'update': ("#FFA500", "Доступно обновление для загрузчика."),
                  'error': (self._color('status_error'), "Не удалось проверить версию загрузчика.")}
        color, tooltip = colors.get(color_key, ("", ""))
        self.loader_status_label.setToolTip(tooltip)
        self.loader_status_label.setStyleSheet(f"color: {color}; font-weight: bold;" if color else "")

    def on_manual_mode_toggled(self, checked):
        self.game_version_combo.setEnabled(checked)
        self.loader_combo.setEnabled(checked)
        if checked:
            current = self.loader_combo.currentText()
            self.loader_combo.blockSignals(True)
            self.loader_combo.clear()
            self.loader_combo.addItems(LOADERS)
            if current in LOADERS:
                self.loader_combo.setCurrentText(current)
            self.loader_combo.blockSignals(False)
        elif self.mods_path and not self._is_switching_profile:
            self.detect_game_versions()
        logging.info(f"Режим ручного ввода {'включен' if checked else 'выключен'}.")

    def current_channel(self) -> UpdateChannel:
        return UpdateChannel.from_value(self.channel_combo.currentData(), app_config.DEFAULT_CHANNEL)

    def _on_channel_changed(self):
        if not self._is_switching_profile and any(m.get('status') != ModStatus.NOT_CHECKED for m in self.mods_data):
            self.statusBar().showMessage("Канал обновлений изменён — нажмите «Сканировать», чтобы применить.", 7000)

    # ------------------------------------------------------------------ Сканирование

    def _is_busy(self) -> bool:
        return any(w and w.isRunning() for w in (self.scan_worker, self.import_worker,
                                                 self.local_scan_worker, self.update_worker))

    def _start_busy_ui(self, label: str):
        self.set_buttons_enabled(False)
        self._setup_cancel_button()
        self.progress_label.setText(label)
        self.progress_bar.setRange(0, 0)
        self.progress_label.show()
        self.progress_bar.show()

    def _stop_busy_ui(self):
        self._reset_scan_button()
        self.progress_label.hide()
        self.progress_bar.hide()

    def scan_mods(self):
        if not self.mods_path or self._is_busy():
            return
        if not self.manual_mode_checkbox.isChecked():
            self.detect_game_versions()  # Сброс версии после возможного импорта
        game_version = self.game_version_combo.currentText()
        loader = self.loader_combo.currentText()
        channel = self.current_channel()
        logging.info(f"Запуск сканирования для Minecraft {game_version} ({loader}), канал {channel.value}, папка {self.mods_path}")

        self._start_busy_ui("Сканирование...")
        self.scan_worker = ScanWorker(self.mods_path, game_version, loader, self.cache_file, channel)
        self.scan_worker.finished.connect(self.on_scan_finished)
        self.scan_worker.progress.connect(self.update_scan_status)
        self.scan_worker.start()

    def start_local_scan(self):
        if not self.mods_path or self._is_busy():
            return
        self._start_busy_ui("Чтение локальных модов...")
        self.local_scan_worker = LocalScanWorker(self.mods_path, self.cache_file)
        self.local_scan_worker.finished.connect(self.on_local_scan_finished)
        self.local_scan_worker.start()

    def _finish_load(self, mods_data, is_import_mode: bool, message: str):
        """Общая обработка результатов сканирования/импорта. mods_data=None — операция отменена."""
        self._stop_busy_ui()
        if mods_data is None:
            self.statusBar().showMessage("Операция отменена.", 5000)
        else:
            self.mods_data = mods_data
            self.is_import_mode = is_import_mode
            if self.current_profile_name:
                self.profile_sessions[self.current_profile_name] = ProfileSession(mods_data, is_import_mode)
            self.statusBar().showMessage(message, 5000)
        self.populate_table()
        self.set_buttons_enabled(True)

    def on_local_scan_finished(self, mods_data):
        self.local_scan_worker = None
        self._finish_load(mods_data, False, f"Найдено {len(mods_data or [])} локальных модов. "
                                            f"Нажмите 'Сканировать' для проверки обновлений.")

    def on_scan_finished(self, mods_data):
        self.scan_worker = None
        if mods_data is not None:
            updates = sum(1 for m in mods_data if m['status'] == ModStatus.UPDATE_AVAILABLE)
            logging.info(f"Сканирование завершено. Найдено {len(mods_data)} модов. Доступно обновлений: {updates}")
        self._finish_load(mods_data, False, f"Сканирование завершено. Найдено {len(mods_data or [])} модов.")

    def on_import_finished(self, mods_data):
        self.import_worker = None
        if mods_data is not None:
            logging.info(f"Импорт завершен. Найдено {len(mods_data)} модов для установки.")
        self._finish_load(mods_data, True, f"Импорт завершен. Готово к установке {len(mods_data or [])} модов.")

    def update_scan_status(self, message: str):
        """Показывает прогресс. Сообщения вида «...: 5 / 100» превращаются в определённый прогресс."""
        self.progress_label.setText(message)
        match = re.search(r'(\d+)\s*/\s*(\d+)', message)
        if match:
            current, total = int(match.group(1)), int(match.group(2))
            if self.progress_bar.maximum() != total:
                self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(current)
        elif self.progress_bar.maximum() != 0:
            self.progress_bar.setRange(0, 0)

    def force_refresh_list(self):
        if self.mods_path:
            logging.info("Принудительное обновление списка модов...")
            self.profile_sessions.pop(self.current_profile_name, None)
            self.start_local_scan()

    # ------------------------------------------------------------------ Кнопка «Сканировать»/«Отмена»

    def _setup_cancel_button(self):
        self.scan_btn.setText("Отмена")
        self.scan_btn.setIcon(self._std_icon(QStyle.StandardPixmap.SP_DialogCancelButton))
        self.scan_btn.clicked.disconnect()
        self.scan_btn.clicked.connect(self.cancel_operation)
        self.scan_btn.setEnabled(True)

    def _reset_scan_button(self):
        self.scan_btn.setText("Сканировать")
        self.scan_btn.setIcon(self._std_icon(QStyle.StandardPixmap.SP_BrowserReload))
        self.scan_btn.clicked.disconnect()
        self.scan_btn.clicked.connect(self.scan_mods)

    def cancel_operation(self):
        if not confirm(self, 'Подтверждение отмены', "Вы уверены, что хотите прервать текущую операцию?"):
            return
        worker = next((w for w in (self.scan_worker, self.import_worker, self.local_scan_worker, self.update_worker)
                       if w and w.isRunning()), None)
        if worker:
            self.scan_btn.setEnabled(False)
            self.statusBar().showMessage("Отмена операции...")
            worker.stop()
        else:
            self._stop_busy_ui()
            self.set_buttons_enabled(True)

    # ------------------------------------------------------------------ Таблица

    def _icon_for(self, data: dict) -> Optional[QIcon]:
        project_id = data.get('project_id')
        if not project_id:
            return None
        if project_id in self._icons:
            return self._icons[project_id]
        path = self.icon_loader.request(project_id, data.get('icon_url'))
        if path:
            self._icons[project_id] = QIcon(path)
            return self._icons[project_id]
        return None

    def _on_icon_ready(self, project_id: str, path: str):
        icon = QIcon(path)
        self._icons[project_id] = icon
        for row in self.project_rows_map.get(project_id, []):
            item = self.table.item(row, TableColumn.NAME)
            if item:
                item.setIcon(icon)

    def _register_row(self, row: int, data: dict):
        self.row_to_data_map[row] = data
        if data.get('project_id'):
            self.project_rows_map.setdefault(data['project_id'], []).append(row)

    def _create_action_widget(self, data, mod_index=None, dep_index=None, main_mod_index=None):
        """Виджет с кнопками действий для строки таблицы."""
        widget = QWidget()
        widget.setStyleSheet("background-color: transparent;")
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(5)
        SP = QStyle.StandardPixmap

        def add_button(pixmap, tooltip, slot, **props):
            btn = QPushButton()
            btn.setIcon(self._std_icon(pixmap))
            btn.setToolTip(tooltip)
            btn.setFixedWidth(30)
            for key, value in props.items():
                if value is not None:
                    btn.setProperty(key, value)
            btn.clicked.connect(slot)
            layout.addWidget(btn)

        index_props = dict(mod_index=mod_index, dep_index=dep_index, main_mod_index=main_mod_index)
        if data.get('filepath') and os.path.exists(data['filepath']):
            add_button(SP.SP_TrashIcon, "Удалить мод (переместить в бэкап)", self.delete_mod, mod_filepath=data['filepath'])
        if data.get('project_id'):
            add_button(SP.SP_FileDialogInfoView, "Открыть страницу мода на Modrinth", self.open_mod_page,
                       project_id=data['project_id'])
            add_button(SP.SP_FileDialogDetailedView, "Выбрать другую версию (Откат/Даунгрейд)",
                       self.choose_specific_version, project_id=data['project_id'], **index_props)
        if data.get('download_url'):
            is_install = data.get('status') == ModStatus.READY_TO_INSTALL or not data.get('filepath')
            verb = "Установить" if is_install else "Обновить"
            noun = "зависимость" if dep_index is not None else "этот мод"
            add_button(SP.SP_ArrowDown, f"{verb} {noun}", self.action_button_clicked, **index_props)

        if layout.count() == 0:
            return None
        layout.addStretch()
        return widget

    def _make_checkbox_cell(self, checked: bool, enabled: bool, left_margin: int, expander: Optional[QPushButton] = None):
        checkbox = QCheckBox()
        checkbox.setChecked(checked)
        checkbox.setEnabled(enabled)
        checkbox.stateChanged.connect(self.update_master_checkbox_state)
        cell = QWidget()
        cell.setStyleSheet("background-color: transparent;")
        layout = QHBoxLayout(cell)
        layout.setContentsMargins(left_margin, 0, 0, 0)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(checkbox)
        if expander:
            layout.addWidget(expander)
        return cell

    def _set_row_items(self, row: int, values: list, start_col: int, background: Optional[QColor]):
        for col, value in enumerate(values, start=start_col):
            item = value if isinstance(value, QTableWidgetItem) else QTableWidgetItem(str(value))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if background:
                item.setBackground(background)
            self.table.setItem(row, col, item)

    def _create_main_mod_row(self, mod, mod_index):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.mod_index_to_rows_map[mod_index] = row
        self._register_row(row, mod)

        header_item = QTableWidgetItem(str(mod_index + 1))
        header_item.setData(Qt.ItemDataRole.UserRole, str(mod_index + 1))
        self.table.setVerticalHeaderItem(row, header_item)

        expander = QPushButton()
        expander.setIcon(self._std_icon(QStyle.StandardPixmap.SP_ArrowRight))
        expander.setFixedSize(18, 18)
        policy = expander.sizePolicy()
        policy.setRetainSizeWhenHidden(True)  # Чекбокс не смещается, когда кнопка скрыта
        expander.setSizePolicy(policy)
        expander.setVisible(bool(mod.get('dependencies')))
        expander.setProperty("main_row", row)
        expander.setProperty("is_expanded", False)
        expander.clicked.connect(self.toggle_dependencies_visibility)

        checked = mod.get('_is_checked', mod['status'] in INSTALLABLE_STATUSES)
        self.table.setCellWidget(row, TableColumn.CHECKBOX, self._make_checkbox_cell(checked, True, 6, expander))

        color = None
        if mod['status'] == ModStatus.UPDATE_AVAILABLE:
            color = QColor(self._color('update_available'))
        elif mod['status'] == ModStatus.READY_TO_INSTALL:
            color = QColor(self._color('ready_to_install'))

        name_item = QTableWidgetItem(mod['name'])
        if icon := self._icon_for(mod):
            name_item.setIcon(icon)
        self._set_row_items(row, [name_item, mod.get('current_version', ''), mod.get('new_version', ''),
                                  mod.get('new_version_date', ''), mod['status']], TableColumn.NAME, color)

        if action_widget := self._create_action_widget(mod, mod_index=mod_index):
            self.table.setCellWidget(row, TableColumn.ACTION, action_widget)

        if mod.get('dependencies'):
            self.row_to_dependencies_map[row] = [self._create_dependency_row(dep, mod_index, dep_index)
                                                 for dep_index, dep in enumerate(mod['dependencies'])]

    def _create_dependency_row(self, dep, main_mod_index, dep_index):
        dep_row = self.table.rowCount()
        self.table.insertRow(dep_row)
        self._register_row(dep_row, dep)

        number = f"{main_mod_index + 1}.{dep_index + 1}"
        header_item = QTableWidgetItem(number)
        header_item.setData(Qt.ItemDataRole.UserRole, number)
        self.table.setVerticalHeaderItem(dep_row, header_item)

        status = dep.get('status')
        blocked = status in (ModStatus.INCOMPATIBLE, ModStatus.VERSION_CONFLICT)
        # По умолчанию отмечаем только то, что действительно нужно: обновления и обязательные зависимости
        default_checked = bool(dep.get('download_url')) and status in (ModStatus.UPDATE_AVAILABLE, ModStatus.REQUIRED)
        checked = dep.get('_is_checked', default_checked) and not blocked
        self.table.setCellWidget(dep_row, TableColumn.CHECKBOX, self._make_checkbox_cell(checked, not blocked, 28))

        row_color = QColor("#383838") if self.active_theme == 'dark' else QColor("#f7f7f7")
        if status == ModStatus.UPDATE_AVAILABLE:
            row_color = QColor(self._color('update_available'))
        elif status == ModStatus.INCOMPATIBLE:
            row_color = QColor(self._color('update_error'))

        dep_type = dep.get('type')
        icon_text = {"required": "❗", "incompatible": "⛔"}.get(dep_type, "ℹ️")
        original_name = dep.get('name', 'N/A')
        name_item = QTableWidgetItem(f"  {icon_text} {original_name}")
        name_item.setData(Qt.ItemDataRole.UserRole, original_name)
        name_item.setData(Qt.ItemDataRole.UserRole + 1, icon_text)
        if icon := self._icon_for(dep):
            name_item.setIcon(icon)
        self._set_row_items(dep_row, [name_item, dep.get('current_version', ''), dep.get('new_version', ''),
                                      dep.get('new_version_date', ''), status or ""], TableColumn.NAME, row_color)
        if status in (ModStatus.REQUIRED, ModStatus.INCOMPATIBLE, ModStatus.VERSION_CONFLICT):
            self.table.item(dep_row, TableColumn.STATUS).setForeground(QColor(self._color('status_error')))
        if status == ModStatus.INCOMPATIBLE:
            name_item.setToolTip("Этот мод помечен как несовместимый с родительским модом")

        if action_widget := self._create_action_widget(dep, dep_index=dep_index, main_mod_index=main_mod_index):
            self.table.setCellWidget(dep_row, TableColumn.ACTION, action_widget)
        self.table.setRowHidden(dep_row, True)
        return dep_row

    def populate_table(self):
        """Полностью перестраивает таблицу по self.mods_data."""
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(0)
            self.row_to_dependencies_map = {}
            self.row_to_data_map = {}
            self.mod_index_to_rows_map = {}
            self.project_rows_map = {}
            for mod_index, mod in enumerate(self.mods_data):
                self._create_main_mod_row(mod, mod_index)

            has_deps = bool(self.row_to_dependencies_map)
            self.toggle_deps_btn.setEnabled(has_deps)
            self.all_dependencies_expanded = False
            if has_deps:
                self.toggle_all_dependencies()  # По умолчанию зависимости раскрыты (внутри вызывается filter_table)
            else:
                self.toggle_deps_btn.setIcon(self._std_icon(QStyle.StandardPixmap.SP_ArrowDown))
                self.toggle_deps_btn.setToolTip("Раскрыть все зависимости")
                self.filter_table()
        finally:
            self.table.setUpdatesEnabled(True)
        self.update_master_checkbox_state()

    def filter_table(self):
        """Фильтрует таблицу по тексту поиска и режиму просмотра."""
        search_text = self.search_input.text().lower()
        view_mode = self.view_mode_combo.currentIndex()
        flat_mode = view_mode in (1, 2, 3)
        shown_items = set()  # Исключение дубликатов в плоском списке
        updates_were_enabled = self.table.updatesEnabled()
        self.table.setUpdatesEnabled(False)

        for main_row in sorted(self.mod_index_to_rows_map.values()):
            mod_data = self.row_to_data_map.get(main_row, {})
            main_item = self.table.item(main_row, TableColumn.NAME)
            dependency_rows = self.row_to_dependencies_map.get(main_row, [])
            if not main_item:
                continue

            main_matches = search_text in main_item.text().lower()
            status = mod_data.get('status')
            mode_matches = True
            if view_mode == 2:
                mode_matches = status == ModStatus.NOT_FOUND
            elif view_mode == 3:
                mode_matches = status in INSTALLABLE_STATUSES

            matching_dep_rows = set()
            for dep_row in dependency_rows:
                dep_item = self.table.item(dep_row, TableColumn.NAME)
                if not dep_item:
                    continue
                orig_name = dep_item.data(Qt.ItemDataRole.UserRole)
                icon_text = dep_item.data(Qt.ItemDataRole.UserRole + 1)
                if orig_name:
                    dep_item.setText(orig_name if view_mode == 1 else f"  {icon_text} {orig_name}")
                dep_cell = self.table.cellWidget(dep_row, TableColumn.CHECKBOX)
                if dep_cell:
                    dep_cell.layout().setContentsMargins(6 if flat_mode else 28, 0, 0, 0)
                if search_text in dep_item.text().lower():
                    matching_dep_rows.add(dep_row)

            if flat_mode:
                visible = mode_matches and (main_matches or not search_text)
                if visible and view_mode in (1, 3):
                    if view_mode == 1 and not mod_data.get('filepath'):
                        visible = False  # Неустановленные моды (например, из импорта)
                    else:
                        ident = mod_data.get('project_id') or mod_data.get('filepath')
                        if ident in shown_items:
                            visible = False
                        elif ident:
                            shown_items.add(ident)
            else:
                visible = mode_matches and (main_matches or bool(matching_dep_rows) or not search_text)
            self.table.setRowHidden(main_row, not visible)

            cell = self.table.cellWidget(main_row, TableColumn.CHECKBOX)
            expander = cell.findChild(QPushButton) if cell else None
            if flat_mode:
                if expander:
                    expander.hide()
                for dep_row in dependency_rows:
                    dep_data = self.row_to_data_map.get(dep_row, {})
                    show = False
                    if view_mode in (1, 3) and dep_data.get('filepath'):
                        show = view_mode == 1 or dep_data.get('status') in INSTALLABLE_STATUSES
                        show = show and (not search_text or dep_row in matching_dep_rows)
                        ident = dep_data.get('project_id') or dep_data.get('filepath')
                        if show and ident:
                            if ident in shown_items:
                                show = False
                            else:
                                shown_items.add(ident)
                    self.table.setRowHidden(dep_row, not show)
            elif expander:
                expander.setVisible(bool(mod_data.get('dependencies')))
                is_expanded = expander.property("is_expanded")
                for dep_row in dependency_rows:
                    show = visible and is_expanded and (not search_text or main_matches or dep_row in matching_dep_rows)
                    self.table.setRowHidden(dep_row, not show)

        self.toggle_deps_btn.setVisible(bool(self.row_to_dependencies_map) and view_mode == 0)

        visible_count = 1
        for row in range(self.table.rowCount()):
            header_item = self.table.verticalHeaderItem(row)
            if header_item and not self.table.isRowHidden(row):
                if flat_mode:
                    header_item.setText(str(visible_count))
                    visible_count += 1
                else:
                    header_item.setText(header_item.data(Qt.ItemDataRole.UserRole) or "")
        self.table.setUpdatesEnabled(updates_were_enabled)

    def on_header_clicked(self, logical_index):
        if logical_index in (TableColumn.CHECKBOX, TableColumn.ACTION):
            return
        if self.current_sort_col == logical_index:
            self.current_sort_order = (Qt.SortOrder.DescendingOrder if self.current_sort_order == Qt.SortOrder.AscendingOrder
                                       else Qt.SortOrder.AscendingOrder)
        else:
            self.current_sort_col = logical_index
            self.current_sort_order = Qt.SortOrder.AscendingOrder

        keys = {TableColumn.NAME: 'name', TableColumn.CURRENT_VERSION: 'current_version',
                TableColumn.NEW_VERSION: 'new_version', TableColumn.RELEASE_DATE: 'new_version_date',
                TableColumn.STATUS: 'status'}
        key = keys.get(logical_index)
        self._sync_table_to_mods_data()
        self.mods_data.sort(key=lambda m: str(m.get(key) or '').lower(),
                            reverse=self.current_sort_order == Qt.SortOrder.DescendingOrder)
        self.populate_table()
        self.table.horizontalHeader().setSortIndicator(logical_index, self.current_sort_order)

    def toggle_dependencies_visibility(self):
        button = self.sender()
        main_row = button.property("main_row") if button else None
        if main_row is None or not self.row_to_dependencies_map.get(main_row):
            return
        expanded = not button.property("is_expanded")
        button.setProperty("is_expanded", expanded)
        button.setIcon(self._std_icon(QStyle.StandardPixmap.SP_ArrowDown if expanded else QStyle.StandardPixmap.SP_ArrowRight))
        self.filter_table()

    def toggle_all_dependencies(self):
        self.all_dependencies_expanded = not self.all_dependencies_expanded
        expanding = self.all_dependencies_expanded
        arrow = QStyle.StandardPixmap.SP_ArrowDown if expanding else QStyle.StandardPixmap.SP_ArrowRight
        for main_row in self.row_to_dependencies_map:
            cell = self.table.cellWidget(main_row, TableColumn.CHECKBOX)
            expander = cell.findChild(QPushButton) if cell else None
            if expander:
                expander.setIcon(self._std_icon(arrow))
                expander.setProperty("is_expanded", expanding)
        self.filter_table()
        self.toggle_deps_btn.setIcon(self._std_icon(QStyle.StandardPixmap.SP_ArrowUp if expanding else QStyle.StandardPixmap.SP_ArrowDown))
        self.toggle_deps_btn.setToolTip("Свернуть все зависимости" if expanding else "Раскрыть все зависимости")

    # ------------------------------------------------------------------ Чекбоксы

    def _row_checkbox(self, row: int) -> Optional[QCheckBox]:
        cell = self.table.cellWidget(row, TableColumn.CHECKBOX)
        return cell.findChild(QCheckBox) if cell else None

    def get_enabled_checkboxes(self) -> List[QCheckBox]:
        return [cb for row in range(self.table.rowCount()) if (cb := self._row_checkbox(row)) and cb.isEnabled()]

    def on_master_checkbox_changed(self, state):
        if self.master_checkbox.checkState() == Qt.CheckState.PartiallyChecked:
            return
        is_checked = state == Qt.CheckState.Checked.value
        for cb in self.get_enabled_checkboxes():
            cb.blockSignals(True)
            cb.setChecked(is_checked)
            cb.blockSignals(False)
        self.update_master_checkbox_state()

    def update_master_checkbox_state(self):
        checkboxes = self.get_enabled_checkboxes()
        self.master_checkbox.blockSignals(True)
        if checkboxes:
            checked = sum(1 for cb in checkboxes if cb.isChecked())
            state = (Qt.CheckState.Unchecked if checked == 0 else
                     Qt.CheckState.Checked if checked == len(checkboxes) else Qt.CheckState.PartiallyChecked)
            self.master_checkbox.setCheckState(state)
            self.master_checkbox.setEnabled(True)
            self.master_checkbox.show()
        else:
            self.master_checkbox.setCheckState(Qt.CheckState.Unchecked)
            self.master_checkbox.setEnabled(False)
            self.master_checkbox.hide()
        self.master_checkbox.blockSignals(False)

    def invert_selection(self):
        for cb in self.get_enabled_checkboxes():
            cb.blockSignals(True)
            cb.setChecked(not cb.isChecked())
            cb.blockSignals(False)
        self.update_master_checkbox_state()

    def _sync_table_to_mods_data(self):
        """Сохраняет состояние чекбоксов в данные (перед перестроением таблицы)."""
        for row, data in self.row_to_data_map.items():
            if cb := self._row_checkbox(row):
                data['_is_checked'] = cb.isChecked()

    # ------------------------------------------------------------------ Обновление и установка

    def _collect_mods(self, predicate: Callable[[dict, Optional[QCheckBox]], bool]) -> List[dict]:
        """Уникальные по project_id моды, которые можно скачать и которые проходят фильтр."""
        result: Dict[str, dict] = {}
        for row in range(self.table.rowCount()):
            data = self.row_to_data_map.get(row)
            if not data or not data.get('project_id') or not data.get('download_url'):
                continue
            if data['project_id'] not in result and predicate(data, self._row_checkbox(row)):
                result[data['project_id']] = data
        return list(result.values())

    def _run_bulk_update(self, mods: List[dict], empty_message: str):
        if not mods:
            show_message(self, QMessageBox.Icon.Information, "Нет выбора", empty_message)
            return
        transaction = None
        if self.is_import_mode:
            proceed, transaction = self._ask_import_install_method()
            if not proceed:
                return
        logging.info(f"Запуск обновления для {len(mods)} модов.")
        self.start_update_worker(mods, transaction)

    def update_selected(self):
        mods = self._collect_mods(lambda data, cb: bool(cb and cb.isChecked() and cb.isEnabled()))
        self._run_bulk_update(mods, "Не выбрано ни одного мода, который можно обновить или установить.")

    def update_unselected(self):
        mods = self._collect_mods(lambda data, cb: data.get('status') in BULK_UPDATE_STATUSES
                                  and cb is not None and not cb.isChecked())
        self._run_bulk_update(mods, "Не найдено невыделенных модов для обновления.")

    def update_all(self):
        mods = self._collect_mods(lambda data, cb: data.get('status') in BULK_UPDATE_STATUSES)
        self._run_bulk_update(mods, "Нет доступных обновлений.")

    def _confirm_game_not_running(self, paths: Optional[List[str]] = None) -> bool:
        """Предупреждает, если файлы модов заблокированы (обычно — запущен Minecraft)."""
        while True:
            locked = find_locked_files(paths or [])
            if not locked and not is_game_running(self.mods_path):
                return True
            names = "\n".join(f"• {os.path.basename(p)}" for p in locked[:5])
            text = ("Похоже, Minecraft запущен: файлы модов заблокированы другим процессом.\n"
                    "Закройте игру перед изменением модов, иначе файлы не удастся заменить.")
            if names:
                text += f"\n\nЗаблокированы:\n{names}"
            choice = ask_choice(self, QMessageBox.Icon.Warning, "Игра запущена?", text,
                                ["Проверить снова", "Продолжить всё равно", "Отмена"], default=0)
            if choice == 1:
                return True
            if choice != 0:
                return False

    def start_update_worker(self, mods_to_update: List[dict], transaction: Optional[UpdateTransaction] = None):
        if self._is_busy():
            return
        if transaction is None and not self._confirm_game_not_running([m.get('filepath') for m in mods_to_update]):
            return
        self._sync_table_to_mods_data()
        self.set_buttons_enabled(False)
        self.progress_bar.setRange(0, len(mods_to_update))
        self.progress_bar.setValue(0)
        self.progress_label.setText(f"Обновлено 0 / {len(mods_to_update)}")
        self.progress_bar.show()
        self.progress_label.show()

        self.update_worker = UpdateWorker(mods_to_update, self.mods_path, transaction)
        self.update_worker.progress.connect(self.update_mod_status)
        self.update_worker.download_progress.connect(self.update_download_progress)
        self.update_worker.finished.connect(self.on_update_finished)
        self.update_worker.start()

    def _rows_for_update_index(self, mod_index: int) -> tuple[Optional[dict], List[int]]:
        if not self.update_worker:
            return None, []
        mod_data = self.update_worker.mods_to_update[mod_index]
        return mod_data, self.project_rows_map.get(mod_data.get('project_id'), [])

    def update_mod_status(self, mod_index: int, status_value: str):
        mod_data, rows = self._rows_for_update_index(mod_index)
        if not mod_data:
            return
        status = ModStatus(status_value)

        if status == ModStatus.DOWNLOADING:
            for row in rows:
                bar = QProgressBar()
                bar.setRange(0, 0)
                bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
                bar.setFormat(status.value)
                self.table.setCellWidget(row, TableColumn.STATUS, bar)
            return

        current = self.progress_bar.value() + 1
        self.progress_bar.setValue(current)
        self.progress_label.setText(f"Обновлено {current} / {self.progress_bar.maximum()}")

        is_success = status in (ModStatus.UPDATED, ModStatus.INSTALLED)
        color = (QColor(self._color('updated')) if is_success else
                 QColor(self._color('update_error')) if status.is_error else None)

        for row in rows:
            row_data = self.row_to_data_map[row]
            row_data['status'] = status
            self.table.removeCellWidget(row, TableColumn.STATUS)
            self.table.setItem(row, TableColumn.STATUS, QTableWidgetItem(status.value))

            if is_success:
                new_filename = mod_data.get('new_filename')
                row_data.update({
                    'current_version': mod_data.get('new_version', ''), 'new_version': "", 'new_version_date': "",
                    'download_url': None, 'new_filename': None, 'new_hashes': None, 'installed': True,
                    'filepath': os.path.join(self.mods_path, new_filename) if new_filename else row_data.get('filepath'),
                    'installed_info': mod_data.get('_latest_info') or row_data.get('installed_info'),
                    '_is_checked': False,
                })
                self.table.setItem(row, TableColumn.CURRENT_VERSION, QTableWidgetItem(row_data['current_version']))
                self.table.setItem(row, TableColumn.NEW_VERSION, QTableWidgetItem(""))
                self.table.setItem(row, TableColumn.RELEASE_DATE, QTableWidgetItem(""))
                if cb := self._row_checkbox(row):
                    cb.setChecked(False)
                    cb.setEnabled(False)
                if action_widget := self._create_action_widget(row_data):
                    self.table.setCellWidget(row, TableColumn.ACTION, action_widget)
                else:
                    self.table.removeCellWidget(row, TableColumn.ACTION)

            if color:
                for col in range(self.table.columnCount()):
                    if item := self.table.item(row, col):
                        item.setBackground(color)

    def update_download_progress(self, mod_index: int, downloaded: int, total: int):
        _, rows = self._rows_for_update_index(mod_index)
        for row in rows:
            bar = self.table.cellWidget(row, TableColumn.STATUS)
            if isinstance(bar, QProgressBar):
                if bar.maximum() != total:
                    bar.setRange(0, total)
                bar.setValue(downloaded)
                bar.setFormat(f"{downloaded / 1048576:.1f} / {total / 1048576:.1f} MB")

    def on_update_finished(self):
        worker = self.update_worker
        self.update_worker = None
        self.progress_bar.hide()
        self.progress_label.hide()
        if self.current_profile_name:
            self.profile_sessions[self.current_profile_name] = ProfileSession(self.mods_data, self.is_import_mode)
        self.set_buttons_enabled(True)

        results = worker.results if worker else {}
        failed = [worker.mods_to_update[i]['name'] for i, s in results.items() if s.is_error]
        succeeded = sum(1 for s in results.values() if s in (ModStatus.UPDATED, ModStatus.INSTALLED))
        self.statusBar().showMessage(f"Готово: успешно {succeeded}, ошибок {len(failed)}.", 10000)
        if failed:
            listed = "\n".join(f"• {name}" for name in failed[:10])
            more = f"\n...и ещё {len(failed) - 10}" if len(failed) > 10 else ""
            show_message(self, QMessageBox.Icon.Warning, "Есть ошибки",
                         f"Не удалось обновить {len(failed)} модов:\n{listed}{more}\n\n"
                         "Подробности в логе. Изменения можно отменить через «Управление сборкой → Откатить последнее изменение».")
        elif succeeded > 1:
            show_message(self, QMessageBox.Icon.Information, "Готово",
                         f"Обновлено модов: {succeeded}. Рекомендуется перезапустить сканирование.")

    def set_buttons_enabled(self, enabled: bool):
        """Включает/выключает кнопки с учётом текущего состояния данных."""
        has_data = bool(self.mods_data)
        has_path = bool(self.mods_path)
        self.scan_btn.setEnabled(enabled and has_path)
        self.browse_btn.setEnabled(enabled)
        self.profile_combo.setEnabled(enabled)
        self.import_export_btn.setEnabled(enabled)
        self.action_import.setEnabled(enabled)
        self.action_sync.setEnabled(enabled)
        can_export = enabled and has_data and not self.is_import_mode
        self.action_share.setEnabled(can_export)
        self.action_export_json.setEnabled(can_export)
        self.action_export_mrpack.setEnabled(can_export)
        self.action_rollback.setEnabled(enabled and has_path)
        self.delete_selected_btn.setEnabled(enabled and has_data)
        self.invert_selection_btn.setEnabled(enabled and has_data)
        self.refresh_list_btn.setEnabled(enabled and has_path)

        active, inactive = ((self.install_buttons, self.update_buttons) if self.is_import_mode
                            else (self.update_buttons, self.install_buttons))
        for btn in inactive:
            btn.hide()
        statuses = {m.get('status') for m in self._all_entries()}
        available = enabled and bool(statuses & set(BULK_UPDATE_STATUSES))
        for btn in active:
            btn.show()
            btn.setEnabled(available)

    def _all_entries(self):
        for mod in self.mods_data:
            yield mod
            yield from mod.get('dependencies', [])

    # ------------------------------------------------------------------ Действия в строке

    def open_mod_page(self):
        button = self.sender()
        if project_id := (button.property("project_id") if button else None):
            webbrowser.open(f"https://modrinth.com/mod/{project_id}")

    def _resolve_entry(self, mod_index, dep_index, main_mod_index) -> Optional[dict]:
        try:
            if dep_index is not None and main_mod_index is not None:
                return self.mods_data[main_mod_index]['dependencies'][dep_index]
            if mod_index is not None:
                return self.mods_data[mod_index]
        except (IndexError, KeyError):
            pass
        return None

    def action_button_clicked(self):
        button = self.sender()
        if not button:
            return
        entry = self._resolve_entry(button.property("mod_index"), button.property("dep_index"),
                                    button.property("main_mod_index"))
        if entry and entry.get('download_url'):
            logging.info(f"Запуск индивидуальной операции для мода '{entry['name']}'.")
            self.start_update_worker([entry])

    def _run_task(self, func, args, on_success, on_failure, busy_message: str):
        """Выполняет блокирующую операцию в фоне, не замораживая интерфейс."""
        worker = TaskWorker(func, *args)
        self._task_workers.append(worker)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.statusBar().showMessage(busy_message)

        def cleanup():
            QApplication.restoreOverrideCursor()
            self.statusBar().clearMessage()
            if worker in self._task_workers:
                self._task_workers.remove(worker)

        worker.succeeded.connect(lambda result: (cleanup(), on_success(result)))
        worker.failed.connect(lambda error: (cleanup(), on_failure(error)))
        worker.start()

    def choose_specific_version(self):
        button = self.sender()
        if not button:
            return
        indexes = (button.property("mod_index"), button.property("dep_index"), button.property("main_mod_index"))

        def on_success(versions):
            if not versions:
                show_message(self, QMessageBox.Icon.Warning, "Пусто", "Не найдено доступных версий.")
                return
            dialog = VersionPickerDialog(versions, self)
            if dialog.exec() and dialog.selected_version:
                self._apply_specific_version(dialog.selected_version, *indexes)

        self._run_task(fetch_versions, (button.property("project_id"), self.game_version_combo.currentText(),
                                        self.loader_combo.currentText()), on_success,
                       lambda e: show_message(self, QMessageBox.Icon.Critical, "Ошибка", f"Не удалось получить список версий:\n{e}"),
                       "Загрузка списка версий...")

    def _apply_specific_version(self, version_info, mod_index, dep_index, main_mod_index):
        entry = self._resolve_entry(mod_index, dep_index, main_mod_index)
        primary_file = select_primary_file(version_info)
        if not entry or not primary_file:
            return
        entry.update({
            'new_version': version_info['version_number'],
            'new_version_date': format_date(version_info.get('date_published')),
            'download_url': primary_file['url'], 'new_filename': primary_file['filename'],
            'new_hashes': primary_file.get('hashes'), '_latest_info': version_info,
            'status': ModStatus.READY_TO_INSTALL, '_is_checked': True,
        })
        self._sync_table_to_mods_data()
        entry['_is_checked'] = True
        self.populate_table()
        self.set_buttons_enabled(True)

    # ------------------------------------------------------------------ Удаление и откат

    def _remove_entries_by_filepath(self, filepaths: set):
        norm = {os.path.normcase(os.path.normpath(p)) for p in filepaths}

        def matches(entry):
            return entry.get('filepath') and os.path.normcase(os.path.normpath(entry['filepath'])) in norm

        self.mods_data = [m for m in self.mods_data if not matches(m)]
        for mod in self.mods_data:
            if mod.get('dependencies'):
                mod['dependencies'] = [d for d in mod['dependencies'] if not matches(d)]

    def _delete_files(self, filepaths: List[str]):
        if not self._confirm_game_not_running(filepaths):
            return
        transaction = UpdateTransaction(self.mods_path, "delete")
        removed, errors = remove_mods(filepaths, transaction)
        self._sync_table_to_mods_data()
        self._remove_entries_by_filepath(set(removed))
        if self.current_profile_name:
            self.profile_sessions[self.current_profile_name] = ProfileSession(self.mods_data, self.is_import_mode)
        self.populate_table()
        self.set_buttons_enabled(True)
        self.statusBar().showMessage(f"Удалено файлов: {len(removed)}.", 5000)
        if errors:
            show_message(self, QMessageBox.Icon.Critical, "Ошибка удаления", "\n".join(errors[:10]))

    def delete_mod(self):
        button = self.sender()
        filepath = button.property("mod_filepath") if button else None
        if not filepath:
            return
        name = next((e['name'] for e in self._all_entries() if e.get('filepath') == filepath), os.path.basename(filepath))
        if confirm(self, 'Подтверждение удаления',
                   f"Вы уверены, что хотите удалить мод '{name}'?\n\n"
                   f"Файл будет перемещен в папку бэкапов, удаление можно откатить.", "Да, удалить", "Нет"):
            self._delete_files([filepath])

    def delete_selected_mods(self):
        filepaths = []
        for row, data in self.row_to_data_map.items():
            cb = self._row_checkbox(row)
            if cb and cb.isChecked() and data.get('filepath') and data['filepath'] not in filepaths:
                filepaths.append(data['filepath'])
        if not filepaths:
            show_message(self, QMessageBox.Icon.Information, "Нет выбора", "Нет выбранных установленных модов для удаления.")
            return
        if confirm(self, "Подтверждение", f"Вы уверены, что хотите удалить {len(filepaths)} модов?\n"
                                         "Они будут перемещены в папку бэкапов, удаление можно откатить."):
            self._delete_files(filepaths)

    def rollback_last_change(self):
        if not self.mods_path:
            return
        manifest = get_last_transaction(self.mods_path)
        if not manifest:
            show_message(self, QMessageBox.Icon.Information, "Откат", "Нет операций, которые можно откатить.")
            return
        if not confirm(self, "Откат последнего изменения",
                       f"{describe_transaction(manifest)}\n\nОткатить эту операцию? Установленные ею файлы будут удалены, "
                       "а прежние файлы восстановлены из бэкапа.", "Откатить", "Отмена"):
            return
        if not self._confirm_game_not_running([os.path.join(self.mods_path, f) for f in manifest.get('installed', [])]):
            return
        errors = rollback_transaction(self.mods_path, manifest)
        if errors:
            show_message(self, QMessageBox.Icon.Warning, "Откат выполнен частично", "\n".join(errors[:10]))
        else:
            self.statusBar().showMessage("Откат выполнен.", 5000)
        self.profile_sessions.pop(self.current_profile_name, None)
        self.start_local_scan()

    # ------------------------------------------------------------------ Импорт / экспорт

    def import_mod_list(self):
        dialog = ImportDialog(self)
        if not dialog.exec():
            return
        share_string = dialog.get_share_string().strip()
        if not share_string:
            return
        try:
            self._process_imported_data(decode_share_string(share_string))
        except ValueError as e:
            logging.error(f"Ошибка при импорте списка модов: {e}")
            show_message(self, QMessageBox.Icon.Critical, "Ошибка импорта", f"Не удалось прочитать строку: {e}")

    def sync_from_url(self):
        dialog = SyncDialog(self)
        if not dialog.exec() or not (url := dialog.get_url()):
            return
        if not url.lower().startswith(("http://", "https://")):
            show_message(self, QMessageBox.Icon.Warning, "Ошибка", "Укажите адрес, начинающийся с http:// или https://")
            return
        self._run_task(fetch_json, (url,), self._process_imported_data,
                       lambda e: show_message(self, QMessageBox.Icon.Critical, "Ошибка", f"Не удалось загрузить данные:\n{e}"),
                       "Загрузка данных с сервера...")

    def _process_imported_data(self, share_data):
        try:
            request = parse_share_data(share_data)
        except ValueError as e:
            show_message(self, QMessageBox.Icon.Critical, "Ошибка", f"Не удалось прочитать данные:\n{e}")
            return

        count = len(request.project_ids)
        use_exact = False
        if request.version >= 2:
            strict_text = ("⚠️ Сборка содержит ЖЕСТКУЮ привязку версий." if request.strict
                           else "ℹ️ Сборка не содержит жесткую привязку.")
            choice = ask_choice(self, QMessageBox.Icon.Question, 'Настройки сборки',
                                f"Версия Minecraft: {request.game_version} ({request.loader})\n"
                                f"Модов в списке: {count}\n\n{strict_text}\n\nКак вы хотите установить моды?",
                                ["Точные версии", "Самые новые", "Отмена"], default=0 if request.strict else 1)
            if choice not in (0, 1):
                return
            use_exact = choice == 0
        elif not confirm(self, 'Подтверждение',
                         f"Импортировать {count} модов для MC {request.game_version} ({request.loader})?"):
            return

        self.game_version_combo.setCurrentText(request.game_version)
        if self.loader_combo.findText(request.loader) == -1:
            self.loader_combo.addItem(request.loader)
        self.loader_combo.setCurrentText(request.loader)

        self._start_busy_ui("Обработка сборки...")
        self.import_worker = ImportWorker(request.target_mods, request.game_version, request.loader, use_exact,
                                          self.current_channel())
        self.import_worker.finished.connect(self.on_import_finished)
        self.import_worker.progress.connect(self.update_scan_status)
        self.import_worker.start()

    def _ask_strict_export(self, recipient: str) -> Optional[bool]:
        choice = ask_choice(self, QMessageBox.Icon.Question, "Настройки экспорта",
                            "Вы хотите жестко привязать сборку к текущим версиям модов?\n\n"
                            f"Да - {recipient} точно такие же версии.\nНет - {recipient} самые новые версии.",
                            ["Точные версии", "Всегда новые", "Отмена"], default=1)
        return None if choice not in (0, 1) else choice == 0

    def share_mod_list(self):
        share_data = build_share_data(self.mods_data, self.game_version_combo.currentText(),
                                      self.loader_combo.currentText(), strict=False)
        if not share_data['mods']:
            show_message(self, QMessageBox.Icon.Warning, "Нет данных",
                         "Не найдено модов с Modrinth. Сначала выполните сканирование.")
            return
        strict = self._ask_strict_export("получателю будут предложены")
        if strict is None:
            return
        share_data['strict'] = strict
        encoded = encode_share_string(share_data)
        logging.info(f"Сгенерирована строка для обмена ({len(share_data['mods'])} модов).")
        ShareDialog(encoded, self).exec()

    def export_server_json(self):
        share_data = build_share_data(self.mods_data, self.game_version_combo.currentText(),
                                      self.loader_combo.currentText(), strict=False)
        if not share_data['mods']:
            show_message(self, QMessageBox.Icon.Warning, "Нет данных",
                         "Не найдено модов с Modrinth. Сначала выполните сканирование.")
            return
        strict = self._ask_strict_export("клиенты скачают")
        if strict is None:
            return
        share_data['strict'] = strict
        file_path, _ = QFileDialog.getSaveFileName(self, "Сохранить JSON для сервера", "modpack.json", "JSON Files (*.json)")
        if file_path:
            try:
                save_server_json(file_path, share_data)
                self.statusBar().showMessage(f"Файл сохранен: {os.path.basename(file_path)}", 5000)
            except OSError as e:
                show_message(self, QMessageBox.Icon.Critical, "Ошибка", f"Не удалось сохранить файл:\n{e}")

    def export_mrpack(self):
        game_version = self.game_version_combo.currentText()
        loader = self.loader_combo.currentText()
        # Версия загрузчика: установленная, иначе последняя доступная (формат .mrpack требует конкретную версию)
        loader_version = self.current_loader_version or self.latest_loader_version
        if not loader_version:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                loader_version = get_latest_loader_version(loader, game_version)
            finally:
                QApplication.restoreOverrideCursor()
        if not loader_version:
            show_message(self, QMessageBox.Icon.Warning, "Ошибка",
                         f"Не удалось определить версию загрузчика {loader} для {game_version}.")
            return

        index = build_mrpack_index(self.mods_data, game_version, loader, loader_version)
        if not index['files']:
            show_message(self, QMessageBox.Icon.Warning, "Ошибка",
                         "Не найдено данных для экспорта. Для создания .mrpack моды должны быть проверены через Modrinth.")
            return
        file_path, _ = QFileDialog.getSaveFileName(self, "Сохранить .mrpack", f"Modpack {game_version}.mrpack",
                                                   "Modrinth Modpack (*.mrpack)")
        if not file_path:
            return
        try:
            write_mrpack(file_path, index)
            show_message(self, QMessageBox.Icon.Information, "Успех",
                         f"Сборка ({len(index['files'])} модов) экспортирована в формат .mrpack!\n"
                         "Теперь её можно импортировать в Prism Launcher или Modrinth App.")
        except OSError as e:
            logging.error(f"Ошибка при экспорте mrpack: {e}")
            show_message(self, QMessageBox.Icon.Critical, "Ошибка", f"Не удалось сохранить файл:\n{e}")

    def _ask_import_install_method(self) -> tuple[bool, Optional[UpdateTransaction]]:
        """
        Спрашивает способ установки импортированной сборки.
        Возвращает (продолжать ли, транзакция с бэкапом при «Заменить все»).
        """
        choice = ask_choice(
            self, QMessageBox.Icon.Question, 'Способ установки',
            "<b>Заменить все (рекомендуется):</b><br>Все текущие моды будут перемещены в бэкап, после чего будет "
            "установлена новая сборка. Если что-то пойдёт не так, всё можно вернуть через "
            "«Откатить последнее изменение».<br><br>"
            "<b>Объединить с текущими:</b><br>Моды из списка будут добавлены или обновлены. "
            "Остальные моды в папке останутся нетронутыми.",
            ["Заменить все", "Объединить", "Отмена"], default=0, rich_text=True)
        if choice not in (0, 1):
            return False, None
        if not self.mods_path or not os.path.isdir(self.mods_path):
            show_message(self, QMessageBox.Icon.Critical, "Ошибка", "Путь к папке mods недействителен.")
            return False, None

        jars = [os.path.join(self.mods_path, f) for f in os.listdir(self.mods_path) if f.lower().endswith('.jar')]
        if not self._confirm_game_not_running(jars):
            return False, None

        transaction = UpdateTransaction(self.mods_path, "replace_all" if choice == 0 else "update")
        if choice == 0:
            try:
                move_all_to_backup(self.mods_path, transaction)
                logging.info(f"Все моды перемещены в бэкап: {transaction.dir}")
            except Exception as e:
                logging.error(f"Ошибка при замене папки mods: {e}")
                show_message(self, QMessageBox.Icon.Critical, "Ошибка бэкапа",
                             f"Не удалось переместить моды в бэкап: {e}\n"
                             "Уже перемещённые файлы можно вернуть через «Откатить последнее изменение».")
                return False, None
        return True, transaction

    # ------------------------------------------------------------------ Конфигурация и профили

    def load_config(self):
        cfg = app_config.load_config()
        self.config_theme = cfg.get("theme", "system")
        self.profiles = cfg["profiles"]
        self.current_profile_name = cfg["current_profile"]
        # Первый запуск: подставляем стандартную папку .minecraft
        profile = self.profiles[self.current_profile_name]
        if not profile.get("mods_path"):
            default_dir = app_config.default_minecraft_dir()
            if os.path.isdir(default_dir):
                profile["mods_path"] = default_dir
        self._populate_profiles_combo()

    def save_config(self):
        self._save_ui_to_current_profile()
        try:
            app_config.save_config({"theme": self.config_theme, "current_profile": self.current_profile_name,
                                    "profiles": self.profiles})
            logging.info("Конфигурация успешно сохранена.")
        except OSError as e:
            logging.error(f"Ошибка при сохранении конфигурации: {e}")

    def _populate_profiles_combo(self):
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(list(self.profiles.keys()))
        self.profile_combo.setCurrentText(self.current_profile_name)
        self.profile_combo.blockSignals(False)
        self._apply_profile_to_ui(self.current_profile_name)

    def _apply_profile_to_ui(self, profile_name: str):
        self._is_switching_profile = True
        try:
            prof = self.profiles.get(profile_name) or app_config.default_profile()
            self.manual_mode_checkbox.setChecked(prof.get("manual_mode", False))
            self.game_version_combo.setCurrentText(prof.get("game_version", app_config.DEFAULT_GAME_VERSION))
            self.loader_combo.setCurrentText(prof.get("loader", app_config.DEFAULT_LOADER))
            channel = UpdateChannel.from_value(prof.get("channel"), app_config.DEFAULT_CHANNEL)
            self.channel_combo.setCurrentIndex(max(0, self.channel_combo.findData(channel.value)))
            self._set_path(prof.get("mods_path", ""))
            logging.info(f"Загружен профиль: {profile_name}")
        finally:
            self._is_switching_profile = False

    def _save_ui_to_current_profile(self):
        if self.current_profile_name not in self.profiles:
            return
        self._sync_table_to_mods_data()
        self.profiles[self.current_profile_name] = {
            "mods_path": self.path_input.text(),
            "game_version": self.game_version_combo.currentText(),
            "loader": self.loader_combo.currentText(),
            "manual_mode": self.manual_mode_checkbox.isChecked(),
            "channel": self.current_channel().value,
        }

    def on_profile_changed(self, index: int):
        if index < 0:
            return
        new_profile = self.profile_combo.itemText(index)
        if new_profile == self.current_profile_name:
            return
        if self._is_busy():
            self.profile_combo.blockSignals(True)
            self.profile_combo.setCurrentText(self.current_profile_name)
            self.profile_combo.blockSignals(False)
            return
        self._save_ui_to_current_profile()
        self.current_profile_name = new_profile
        self._apply_profile_to_ui(new_profile)

    def _ask_profile_name(self, title: str, label: str, text: str = "") -> Optional[str]:
        name, ok = QInputDialog.getText(self, title, label, text=text)
        name = name.strip()
        if not ok or not name:
            return None
        if name in self.profiles:
            show_message(self, QMessageBox.Icon.Warning, "Ошибка", "Профиль с таким именем уже существует!")
            return None
        return name

    def add_profile(self):
        if name := self._ask_profile_name("Новый профиль", "Введите имя профиля:"):
            self._save_ui_to_current_profile()
            self.profiles[name] = app_config.default_profile()
            self.current_profile_name = name
            self._populate_profiles_combo()

    def rename_profile(self):
        old_name = self.current_profile_name
        if (name := self._ask_profile_name("Переименовать профиль", "Введите новое имя:", old_name)) and name != old_name:
            self._save_ui_to_current_profile()
            self.profiles = {(name if k == old_name else k): v for k, v in self.profiles.items()}
            if old_name in self.profile_sessions:
                self.profile_sessions[name] = self.profile_sessions.pop(old_name)
            self.current_profile_name = name
            self._populate_profiles_combo()

    def delete_profile(self):
        if len(self.profiles) <= 1:
            show_message(self, QMessageBox.Icon.Warning, "Ошибка", "Нельзя удалить единственный профиль!")
            return
        if confirm(self, "Удаление", f"Удалить профиль '{self.current_profile_name}'?"):
            del self.profiles[self.current_profile_name]
            self.profile_sessions.pop(self.current_profile_name, None)
            self.current_profile_name = next(iter(self.profiles))
            self._populate_profiles_combo()
