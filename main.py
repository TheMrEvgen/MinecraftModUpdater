import os
import sys
import datetime
import logging
import re
import time
import json
import zlib
import base64
import shutil
import webbrowser
import appdirs
import zipfile
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None
from typing import Optional
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QDialog, QInputDialog,
    QLineEdit, QPushButton, QFileDialog, QTableWidget, QTableWidgetItem, QGroupBox, QProgressBar,
    QHeaderView, QCheckBox, QComboBox, QLabel, QMessageBox, QPlainTextEdit, QStyle, QSizePolicy,
    QMenu
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QObject, QEvent, QRect
from PyQt6.QtGui import QColor, QPalette, QAction
from constants import THEME_COLORS, DARK_STYLE, LIGHT_STYLE, TableColumn
from ui_components import (
    TitleBar, CustomMessageBox, ImportDialog, ShareDialog, VersionPickerDialog, SyncDialog
)
from workers import (
    ScanWorker, UpdateWorker, LocalScanWorker, ImportWorker, LoaderVersionWorker
)
from mod_updater_logic import backup_and_remove_mod, get_all_versions

# --- Логирование в GUI ---

class QtLogHandler(logging.Handler):
    """Пользовательский обработчик логов для перенаправления в GUI."""

    def __init__(self, parent_emitter):
        super().__init__()
        self.emitter = parent_emitter

    def emit(self, record):
        msg = self.format(record)
        self.emitter.log_signal.emit(msg)

class LogEmitter(QObject):

    """Класс-эмиттер для сигналов логгера."""
    log_signal = pyqtSignal(str)


# --- Кэш профилей ---

class ProfileSession:
    """Хранит состояние данных и UI для конкретного профиля."""
    def __init__(self, mods_data=None):
        self.mods_data = mods_data if mods_data is not None else []
        self.is_scanned = False
        self.last_scanned = None

class ProfileCacheManager:
    """Менеджер для управления кэшем профилей (In-Memory)."""
    def __init__(self):
        self.sessions: dict[str, ProfileSession] = {}

    def get_session(self, profile_id: str) -> Optional[ProfileSession]:
        return self.sessions.get(profile_id)

    def save_session(self, profile_id: str, mods_data: list):
        if profile_id not in self.sessions:
            self.sessions[profile_id] = ProfileSession()
        self.sessions[profile_id].mods_data = mods_data
        self.sessions[profile_id].is_scanned = True
        self.sessions[profile_id].last_scanned = datetime.datetime.now()

    def clear_session(self, profile_id: str):
        if profile_id in self.sessions:
            del self.sessions[profile_id]


# --- Основное окно приложения ---

class ModUpdaterApp(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Minecraft Mod Updater")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint) # Убираем стандартную рамку
        self.setGeometry(100, 100, 1280, 720)
        self.setMinimumSize(800, 600) # Устанавливаем минимальный размер окна

        self.scan_worker: Optional[ScanWorker] = None
        self.import_worker: Optional[ImportWorker] = None
        self.local_scan_worker: Optional[LocalScanWorker] = None
        self.last_loader_status: Optional[tuple[str, str]] = None
        self.row_to_dependencies_map = {} # Карта для отслеживания дочерних строк зависимостей
        self.mod_index_to_rows_map = {} # Карта для отслеживания строк таблицы для каждого мода
        self.row_to_data_map = {} # Карта для отслеживания данных для каждой строки таблицы
        self.is_import_mode = False
        self.all_dependencies_expanded = False

        self.profiles: dict[str, dict] = {}
        self.current_profile_name: str = ""
        self.profile_cache = ProfileCacheManager() # Менеджер кэша профилей
        self._is_switching_profile = False # Флаг для отличия ручного ввода пути от смены профиля

        # Эмиттер для сигналов логгера
        self.log_emitter = LogEmitter()

        self.config_theme = 'system' # User's preference: 'system', 'light', 'dark'
        self.active_theme = 'light'  # The actually active theme: 'light', 'dark'

        # --- Создание виджетов ---
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget) # Главный макет без отступов
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(0)

        self.title_bar = TitleBar(self)
        
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget) # Макет для контента с отступами

        # Верхняя панель (Профили + Путь + Кнопки)
        top_panel = QHBoxLayout()
        
        top_panel.addWidget(QLabel("Профиль:"))
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(150) # Зададим минимальную ширину для удобства
        self.profile_combo.currentIndexChanged.connect(self.on_profile_changed)
        top_panel.addWidget(self.profile_combo)

        self.add_profile_btn = QPushButton("➕")
        self.add_profile_btn.setToolTip("Добавить профиль")
        self.add_profile_btn.clicked.connect(self.add_profile)
        
        self.rename_profile_btn = QPushButton("✏️")
        self.rename_profile_btn.setToolTip("Переименовать профиль")
        self.rename_profile_btn.clicked.connect(self.rename_profile)
        
        self.delete_profile_btn = QPushButton("🗑️")
        self.delete_profile_btn.setToolTip("Удалить профиль")
        self.delete_profile_btn.clicked.connect(self.delete_profile)

        top_panel.addWidget(self.add_profile_btn)
        top_panel.addWidget(self.rename_profile_btn)
        top_panel.addWidget(self.delete_profile_btn)
        
        # Добавим разделитель между профилями и выбором пути
        top_panel.addSpacing(15)

        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("Выберите папку с игрой (где находится папка mods)")
        
        self.browse_btn = QPushButton("Обзор...")
        self.browse_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon))
        self.open_mods_folder_btn = QPushButton("Открыть папку mods")
        self.open_mods_folder_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        self.status_label = QLabel("Папка mods не найдена")

        # Добавляем инпут пути с растяжением (1), чтобы он занимал оставшееся свободное место
        top_panel.addWidget(self.path_input, 1)
        top_panel.addWidget(self.browse_btn)
        top_panel.addWidget(self.open_mods_folder_btn)
        top_panel.addWidget(self.status_label)
        
        # Небольшой отступ перед кнопками импорта/экспорта
        top_panel.addSpacing(15)

        # Кнопка с выпадающим меню Импорта/Экспорта
        self.import_export_btn = QPushButton("Управление сборкой ▼")
        self.import_export_menu = QMenu(self)
        
        self.action_import = QAction(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown), "Импортировать список", self)
        self.action_sync = QAction(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload), "Синхронизация по URL", self)
        self.action_export_json = QAction(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton), "Экспорт JSON", self)
        self.action_share = QAction(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowUp), "Поделиться списком", self)
        self.action_export_mrpack = QAction(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton), "Экспорт в .mrpack", self)
        self.action_export_mrpack.setToolTip("Экспорт сборки для Modrinth App или Prism Launcher")
        
        self.action_export_json.setEnabled(False)
        self.action_share.setEnabled(False)
        self.action_export_mrpack.setEnabled(False)

        self.import_export_menu.addAction(self.action_import)
        self.import_export_menu.addAction(self.action_sync)
        self.import_export_menu.addSeparator()
        self.import_export_menu.addAction(self.action_export_json)
        self.import_export_menu.addAction(self.action_share)
        self.import_export_menu.addAction(self.action_export_mrpack)

        self.import_export_btn.setMenu(self.import_export_menu)
        top_panel.addWidget(self.import_export_btn)

        # Панель настроек
        settings_group = QGroupBox("Настройки сканирования")
        settings_panel = QHBoxLayout(settings_group)
        settings_panel.addWidget(QLabel("Версия игры:"))
        self.game_version_combo = QComboBox()
        self.game_version_combo.setEditable(True)
        settings_panel.addWidget(self.game_version_combo, 1)
        
        settings_panel.addWidget(QLabel("Загрузчик:"))
        self.loader_combo = QComboBox()
        self.loader_combo.addItems(["fabric", "forge", "neoforge", "quilt"])
        settings_panel.addWidget(self.loader_combo, 1)

        self.loader_status_label = QLabel("")
        self.loader_status_label.setToolTip("Статус версии загрузчика (проверяется автоматически)")
        settings_panel.addWidget(self.loader_status_label)

        self.manual_mode_checkbox = QCheckBox("Ручной режим")
        settings_panel.addWidget(self.manual_mode_checkbox)

        settings_panel.addStretch(4)

        # Изначально контролы версий отключены (для авто-режима)
        self.game_version_combo.setEnabled(False)
        self.loader_combo.setEnabled(False)

        # Центральная часть (таблица)
        self.mods_data: list = []
        self.table = QTableWidget()
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setColumnCount(len(TableColumn))
        self.table.setHorizontalHeaderLabels(["", "Название мода", "Текущая версия", "Новая версия", "Дата выхода", "Статус", "Действие"])
        
        # Делаем ВСЕ столбцы интерактивными для ручного изменения размера (Column Resizing)
        self.table.horizontalHeader().setSectionResizeMode(TableColumn.NAME, QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(TableColumn.CHECKBOX, QHeaderView.ResizeMode.Interactive) # Фиксированная ширина ниже
        self.table.horizontalHeader().setSectionResizeMode(TableColumn.ACTION, QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(TableColumn.CURRENT_VERSION, QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(TableColumn.NEW_VERSION, QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(TableColumn.RELEASE_DATE, QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(TableColumn.STATUS, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(TableColumn.CURRENT_VERSION, 110)
        self.table.setColumnWidth(TableColumn.NEW_VERSION, 110)
        self.table.setColumnWidth(TableColumn.RELEASE_DATE, 90)
        self.table.setColumnWidth(TableColumn.STATUS, 150)
        self.table.setColumnWidth(TableColumn.ACTION, 110) # Фиксированная ширина, достаточная для 3х кнопок
        self.table.horizontalHeader().setStretchLastSection(True) # Последняя колонка заполняет остаток ширины окна
        self.table.setColumnWidth(TableColumn.CHECKBOX, 60)  # Для чекбокса и кнопки [+]
        # Стиль для заголовка таблицы будет устанавливаться в apply_theme

        # Создаем главный чекбокс и размещаем его прямо в заголовке таблицы
        self.master_checkbox = QCheckBox(self.table.horizontalHeader())
        self.master_checkbox.setTristate(True)
        self.master_checkbox.hide() # Изначально скрыт, чтобы был виден "№"

        # Создаем кнопку "Раскрыть все" и размещаем ее в заголовке
        self.toggle_deps_btn = QPushButton(self.table.horizontalHeader())
        self.toggle_deps_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown))
        self.toggle_deps_btn.setToolTip("Раскрыть все зависимости")
        self.toggle_deps_btn.clicked.connect(self.toggle_all_dependencies)
        self.toggle_deps_btn.setFixedSize(22, 22)
        self.toggle_deps_btn.setText("")
        self.toggle_deps_btn.hide() # Изначально скрыта

        # Нижняя панель
        self.bottom_panel = QHBoxLayout() # type: ignore
        self.scan_btn = QPushButton("Сканировать")
        self.scan_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))

        # Кнопки для обновления
        self.update_selected_btn = QPushButton("Обновить выбранные")
        self.update_selected_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown))
        self.update_unselected_btn = QPushButton("Обновить всё, кроме выбранных")
        self.update_unselected_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowUp))
        self.update_all_btn = QPushButton("Обновить все")
        self.update_all_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaSeekForward))

        # Кнопки для установки
        self.install_selected_btn = QPushButton("Установить выбранные")
        self.install_selected_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown))
        self.install_unselected_btn = QPushButton("Установить всё, кроме выбранных")
        self.install_unselected_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowUp))
        self.install_all_btn = QPushButton("Установить все")
        self.install_all_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaSeekForward))

        self.scan_btn.setEnabled(False)
        self.update_selected_btn.setEnabled(False)
        self.update_unselected_btn.setEnabled(False)
        self.update_all_btn.setEnabled(False)
        self.install_selected_btn.setEnabled(False)
        self.install_unselected_btn.setEnabled(False)
        self.install_all_btn.setEnabled(False)

        # Изначально показываем кнопки обновления, скрываем кнопки установки
        self.install_selected_btn.hide()
        self.install_unselected_btn.hide()
        self.install_all_btn.hide()

        self.bottom_panel.addWidget(self.scan_btn)
        self.bottom_panel.addWidget(self.update_selected_btn)
        self.bottom_panel.addWidget(self.update_unselected_btn)
        self.bottom_panel.addWidget(self.update_all_btn)
        self.bottom_panel.addWidget(self.install_selected_btn)
        self.bottom_panel.addWidget(self.install_unselected_btn)
        self.bottom_panel.addWidget(self.install_all_btn)

        # Панель логов
        self.log_console = QPlainTextEdit()
        self.log_console.setReadOnly(True)
        self.log_console.setVisible(False) # Изначально скрыта
        self.toggle_log_btn = QPushButton("Показать/скрыть лог")
        self.toggle_log_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation))
        self.toggle_log_btn.setCheckable(True)

        # Сборка интерфейса
        layout.addWidget(self.title_bar)
        layout.addWidget(content_widget)
        content_layout.addLayout(top_panel)
        content_layout.addWidget(settings_group)

        # Панель поиска
        search_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Поиск по названию мода...")
        
        self.view_mode_combo = QComboBox()
        self.view_mode_combo.addItems(["Вид: Иерархия (С зависимостями)", "Вид: Простой список (Мои моды)", "Фильтр: Не найдены на Modrinth", "Фильтр: Нуждаются в обновлении"])
        self.view_mode_combo.currentIndexChanged.connect(self.filter_table)
        
        search_layout.addWidget(self.search_input)
        search_layout.addWidget(self.view_mode_combo)
        content_layout.addLayout(search_layout)
        
        # Панель действий над таблицей
        table_actions_layout = QHBoxLayout()
        self.invert_selection_btn = QPushButton("Инвертировать выделение")
        self.invert_selection_btn.setToolTip("Снять выделение с выбранных и выделить невыбранные")
        self.delete_selected_btn = QPushButton("Удалить выбранные")
        self.delete_selected_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
        self.refresh_list_btn = QPushButton("Обновить список (Force Refresh)")
        self.refresh_list_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.refresh_list_btn.setToolTip("Принудительно пересканировать папку (игнорируя кэш)")

        self.invert_selection_btn.setEnabled(False)
        self.delete_selected_btn.setEnabled(False)
        self.refresh_list_btn.setEnabled(False)

        table_actions_layout.addWidget(self.invert_selection_btn)
        table_actions_layout.addWidget(self.delete_selected_btn)
        table_actions_layout.addStretch()
        table_actions_layout.addWidget(self.refresh_list_btn)
        content_layout.addLayout(table_actions_layout)

        content_layout.addWidget(self.table)
        content_layout.addLayout(self.bottom_panel)
        content_layout.addWidget(self.toggle_log_btn)
        content_layout.addWidget(self.log_console)

        # --- Виджеты для статус-бара ---
        self.progress_label = QLabel("")
        self.progress_bar = QProgressBar()
        self.progress_label.hide()
        self.progress_bar.hide()
        self.progress_bar.setMaximumWidth(300) # Ограничиваем ширину
        self.progress_bar.setTextVisible(False) # Текст будет в отдельном QLabel

        # Добавляем виджеты прогресса в правую часть статус-бара
        self.statusBar().addPermanentWidget(self.progress_label)
        self.statusBar().addPermanentWidget(self.progress_bar)
        self.statusBar().setSizeGripEnabled(True)

        # Настройка обработчика логов для GUI
        self.setup_gui_logging()

        # --- Подключение сигналов ---
        self.open_mods_folder_btn.clicked.connect(self.open_mods_folder)
        self.browse_btn.clicked.connect(self.browse_folder)
        self.path_input.textChanged.connect(self.check_mods_folder)
        self.scan_btn.clicked.connect(self.scan_mods)
        
        self.update_selected_btn.clicked.connect(self.update_selected)
        self.install_selected_btn.clicked.connect(self.update_selected) # Использует тот же метод
        self.update_all_btn.clicked.connect(self.update_all)
        self.install_all_btn.clicked.connect(self.update_all) # Использует тот же метод
        self.update_unselected_btn.clicked.connect(self.update_unselected)
        self.install_unselected_btn.clicked.connect(self.update_unselected)
        self.master_checkbox.stateChanged.connect(self.on_master_checkbox_changed)
        self.toggle_log_btn.toggled.connect(self.log_console.setVisible)
        self.manual_mode_checkbox.toggled.connect(self.on_manual_mode_toggled)
        self.search_input.textChanged.connect(self.filter_table)

        self.table.horizontalHeader().sectionResized.connect(self.on_section_resized)
        self.table.horizontalHeader().sectionClicked.connect(self.on_header_clicked)
        self.table.horizontalHeader().setSortIndicatorShown(True)
        self.current_sort_col = -1
        self.current_sort_order = Qt.SortOrder.AscendingOrder

        self.action_import.triggered.connect(self.import_mod_list)
        self.action_sync.triggered.connect(self.sync_from_url)
        self.action_export_json.triggered.connect(self.export_server_json)
        self.action_share.triggered.connect(self.share_mod_list)
        self.action_export_mrpack.triggered.connect(self.export_mrpack)

        self.invert_selection_btn.clicked.connect(self.invert_selection)
        self.delete_selected_btn.clicked.connect(self.delete_selected_mods)
        self.refresh_list_btn.clicked.connect(self.force_refresh_list)

        self.check_default_minecraft_path()

    def keyPressEvent(self, event):
        """Обрабатывает нажатия клавиш, в частности исправляет баг с Alt+F4."""
        if event.key() == Qt.Key.Key_F4 and event.modifiers() == Qt.KeyboardModifier.AltModifier:
            self.close()
            event.accept()
        else:
            super().keyPressEvent(event)

    def showEvent(self, event):
        """Вызывается при первом показе окна. Используется для корректного позиционирования виджетов."""
        super().showEvent(event)
        self.load_config()
        self.apply_theme()

        # Позиционируем виджеты в заголовке только один раз, когда размеры окна и виджетов уже известны
        if not hasattr(self, '_header_widgets_positioned'):
            self.reposition_header_widgets()
            self._header_widgets_positioned = True # type: ignore

    def reposition_header_widgets(self):
        """(Пере)позиционирует виджеты (чекбокс, кнопка раскрытия) в заголовке первого столбца."""
        header_height = self.table.horizontalHeader().height()

        # Фиксированный отступ слева для выравнивания со строками
        start_x = 6
        cb_width = self.master_checkbox.sizeHint().width()
        cb_y = (header_height - self.master_checkbox.sizeHint().height()) // 2
        
        btn_y = (header_height - self.toggle_deps_btn.height()) // 2
        
        self.master_checkbox.move(start_x, cb_y)
        self.toggle_deps_btn.move(start_x + cb_width + 4, btn_y)

    def on_section_resized(self, logicalIndex, oldSize, newSize):
        """Вызывается при изменении размера столбца."""
        if logicalIndex == TableColumn.CHECKBOX:
            self.reposition_header_widgets()

    def on_header_clicked(self, logicalIndex):
        """Обрабатывает клик по заголовку таблицы для сортировки."""
        if logicalIndex in [TableColumn.CHECKBOX, TableColumn.ACTION]:
            return # Не сортируем по этим колонкам
            
        if self.current_sort_col == logicalIndex:
            self.current_sort_order = (Qt.SortOrder.DescendingOrder if self.current_sort_order == Qt.SortOrder.AscendingOrder else Qt.SortOrder.AscendingOrder)
        else:
            self.current_sort_col = logicalIndex
            self.current_sort_order = Qt.SortOrder.AscendingOrder

        reverse = self.current_sort_order == Qt.SortOrder.DescendingOrder

        self._sync_table_to_mods_data() # Сохраняем состояние чекбоксов перед сортировкой

        # Сортируем основной список
        def sort_key(mod):
            if logicalIndex == TableColumn.NAME: return mod.get('name', '').lower()
            elif logicalIndex == TableColumn.CURRENT_VERSION: return mod.get('current_version', '')
            elif logicalIndex == TableColumn.NEW_VERSION: return mod.get('new_version', '')
            elif logicalIndex == TableColumn.RELEASE_DATE: return mod.get('new_version_date', '')
            elif logicalIndex == TableColumn.STATUS: return mod.get('status', '')
            return ''
            
        self.mods_data.sort(key=sort_key, reverse=reverse)
        
        self.populate_table()
        self.table.horizontalHeader().setSortIndicator(logicalIndex, self.current_sort_order)

    def filter_table(self):
        """Фильтрует таблицу по тексту и режиму просмотра."""
        search_text = self.search_input.text().lower()
        view_mode = getattr(self, 'view_mode_combo', None)
        view_mode_index = view_mode.currentIndex() if view_mode else 0
        
        shown_items = set() # Для исключения дубликатов в плоском списке (по project_id или filepath)

        for main_row in range(self.table.rowCount()):
            cell_widget = self.table.cellWidget(main_row, TableColumn.CHECKBOX)
            # У основных строк всегда есть кнопка раскрытия (expander_button) типа QPushButton,
            # в то время как у зависимостей в этом виджете только QCheckBox.
            if not cell_widget or not cell_widget.findChild(QPushButton):
                continue  # Это строка зависимости, пропускаем

            mod_data = self.row_to_data_map.get(main_row, {})
            main_mod_item = self.table.item(main_row, TableColumn.NAME)
            if not main_mod_item:
                self.table.setRowHidden(main_row, True)
                for dep_row in self.row_to_dependencies_map.get(main_row, []):
                    self.table.setRowHidden(dep_row, True)
                continue

            main_mod_matches = search_text in main_mod_item.text().lower()

            # Фильтрация по режиму просмотра
            mode_matches = True
            if view_mode_index == 2:  # Не найдены на Modrinth
                if mod_data.get('status') != "Не найден на Modrinth":
                    mode_matches = False
            elif view_mode_index == 3:  # Нуждаются в обновлении
                if mod_data.get('status') not in ["Доступно обновление", "Готов к установке"]:
                    mode_matches = False

            dependency_rows = self.row_to_dependencies_map.get(main_row, [])
            matching_dependency_rows = []
            
            for dep_row in dependency_rows:
                dep_data = self.row_to_data_map.get(dep_row, {})
                dep_item = self.table.item(dep_row, TableColumn.NAME)
                
                if not dep_item: continue

                # Убираем или возвращаем отступы и иконки для зависимостей
                orig_name = dep_item.data(Qt.ItemDataRole.UserRole)
                icon = dep_item.data(Qt.ItemDataRole.UserRole + 1)
                
                if orig_name:
                    if view_mode_index == 1:
                        dep_item.setText(orig_name)
                    else:
                        dep_item.setText(f"  {icon} {orig_name}")

                # Убираем визуальное смещение колонки чекбокса в простом списке
                dep_cell_widget = self.table.cellWidget(dep_row, TableColumn.CHECKBOX)
                if dep_cell_widget:
                    dep_layout = dep_cell_widget.layout()
                    if view_mode_index in [1, 2, 3]:
                        dep_layout.setContentsMargins(6, 0, 0, 0)
                    else:
                        dep_layout.setContentsMargins(28, 0, 0, 0)

                if dep_item and search_text in dep_item.text().lower():
                    matching_dependency_rows.append(dep_row)

            if view_mode_index in [1, 2, 3]: # Простой список или фильтр
                main_row_is_visible = mode_matches and (main_mod_matches or search_text == "")
                if view_mode_index in [1, 3] and main_row_is_visible:
                    # Скрываем не установленные основные моды (например, из списка импорта)
                    if view_mode_index == 1 and mod_data.get('filepath') is None and not mod_data.get('installed', False):
                        main_row_is_visible = False
                    else:
                        # Предотвращаем дубликаты
                        ident = mod_data.get('project_id') or mod_data.get('filepath')
                        if ident:
                            if ident in shown_items:
                                main_row_is_visible = False
                            else:
                                shown_items.add(ident)
            else: # Иерархия
                main_row_is_visible = mode_matches and (main_mod_matches or len(matching_dependency_rows) > 0 or search_text == "")

            self.table.setRowHidden(main_row, not main_row_is_visible)

            expander_button = cell_widget.findChild(QPushButton)
            
            if view_mode_index in [1, 2, 3]:
                # В упрощенных режимах скрываем кнопку раскрытия и все зависимости
                if expander_button: expander_button.hide()
                for dep_row in dependency_rows:
                    dep_data = self.row_to_data_map.get(dep_row, {})
                    is_installed = dep_data.get('filepath') is not None or dep_data.get('installed', False)
                    
                    if view_mode_index in [1, 3] and is_installed:
                        if view_mode_index == 3 and dep_data.get('status') not in ["Доступно обновление", "Готов к установке"]:
                            self.table.setRowHidden(dep_row, True)
                            continue
                            
                        # В плоском списке показываем установленные зависимости как обычные моды
                        show_this_dep = (search_text == "" or dep_row in matching_dependency_rows)
                        if show_this_dep:
                            ident = dep_data.get('project_id') or dep_data.get('filepath')
                            if ident:
                                if ident in shown_items:
                                    show_this_dep = False
                                else:
                                    shown_items.add(ident)
                        self.table.setRowHidden(dep_row, not show_this_dep)
                    else:
                        self.table.setRowHidden(dep_row, True)
            else:
                # В режиме иерархии
                if expander_button:
                    expander_button.setVisible('dependencies' in mod_data and bool(mod_data['dependencies']))
                    is_expanded = expander_button.property("is_expanded")
                    for dep_row in dependency_rows:
                        if not main_row_is_visible or not is_expanded:
                            self.table.setRowHidden(dep_row, True)
                            continue
                        show_this_dep = (search_text == "" or main_mod_matches or dep_row in matching_dependency_rows)
                        self.table.setRowHidden(dep_row, not show_this_dep)

        has_deps = bool(self.row_to_dependencies_map)
        self.toggle_deps_btn.setVisible(has_deps and view_mode_index == 0)
        
        # Пересчет нумерации строк
        visible_count = 1
        for row in range(self.table.rowCount()):
            if not self.table.isRowHidden(row):
                header_item = self.table.verticalHeaderItem(row)
                if header_item:
                    if view_mode_index in [1, 2, 3]:
                        header_item.setText(str(visible_count))
                        visible_count += 1
                    else:
                        orig_num = header_item.data(Qt.ItemDataRole.UserRole)
                        if orig_num:
                            header_item.setText(orig_num)

    def _setup_cancel_button(self):
        """Настраивает кнопку 'Сканировать' как кнопку 'Отмена'."""
        self.scan_btn.setText("Отмена")
        self.scan_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogCancelButton))
        try:
            self.scan_btn.clicked.disconnect()
        except TypeError:
            pass  # Сигналы не были подключены
        self.scan_btn.clicked.connect(self.cancel_operation)
        self.scan_btn.setEnabled(True)

    def _reset_scan_button(self):
        """Возвращает кнопку 'Сканировать' в исходное состояние."""
        self.scan_btn.setText("Сканировать")
        self.scan_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        try:
            self.scan_btn.clicked.disconnect()
        except TypeError:
            pass
        self.scan_btn.clicked.connect(self.scan_mods)
        # Состояние enabled будет установлено вызывающей функцией

    def cancel_operation(self):
        """Показывает подтверждение и останавливает текущую фоновую операцию."""
        msg_box = CustomMessageBox(QMessageBox.Icon.Question,
                                   'Подтверждение отмены',
                                   "Вы уверены, что хотите прервать текущую операцию?",
                                   self)
        yes_button = msg_box.addButton("Да", QMessageBox.ButtonRole.YesRole)
        no_button = msg_box.addButton("Нет", QMessageBox.ButtonRole.NoRole)
        msg_box.setDefaultButton(no_button)
        msg_box.exec()

        # `accept()` вызывается для YesRole, `reject()` для NoRole
        if msg_box.result() == QDialog.DialogCode.Accepted:
            self.scan_btn.setEnabled(False)  # Блокируем кнопку от повторных нажатий
            self.statusBar().showMessage("Отмена операции...")
            worker_to_stop = None
            if self.scan_worker and self.scan_worker.isRunning():
                worker_to_stop = self.scan_worker
            elif self.import_worker and self.import_worker.isRunning():
                worker_to_stop = self.import_worker # type: ignore
            elif self.local_scan_worker and self.local_scan_worker.isRunning():
                worker_to_stop = self.local_scan_worker
            
            if worker_to_stop:
                worker_to_stop.stop()
            else:
                logging.warning("Кнопка отмены нажата, но нет активных операций для отмены.")
                self._reset_scan_button()
                self.set_buttons_enabled(True)

    def on_loader_version_check_finished(self, message, color_key):
        """Обновляет UI по результатам проверки версии загрузчика."""
        self.last_loader_status = (message, color_key) # Сохраняем состояние для смены темы
        self.loader_status_label.setText(message)
        
        color = ""
        tooltip_text = ""
        if color_key == 'ok':
            color = THEME_COLORS[self.active_theme]['status_ok']
            tooltip_text = "Версия загрузчика актуальна."
        elif color_key == 'update':
            color = "#FFA500" # Orange, works on both themes
            tooltip_text = "Доступно обновление для загрузчика."
        elif color_key == 'error':
            color = THEME_COLORS[self.active_theme]['status_error']
            tooltip_text = "Не удалось проверить версию загрузчика."
        
        self.loader_status_label.setToolTip(tooltip_text)

        if color:
            self.loader_status_label.setStyleSheet(f"color: {color}; font-weight: bold;")
        else:
            self.loader_status_label.setStyleSheet("")

    def get_windows_theme(self) -> Optional[str]:
        """Проверяет реестр Windows на предмет темы приложений. Возвращает 'dark', 'light' или None."""
        if sys.platform != "win32":
            return None
        try:
            import winreg
            key_path = r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize'
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path)
            value, _ = winreg.QueryValueEx(key, 'AppsUseLightTheme')
            winreg.CloseKey(key)
            if value == 0:
                logging.info("Системная тема (Windows): обнаружена тёмная тема через реестр.")
                return 'dark'
            else:
                logging.info("Системная тема (Windows): обнаружена светлая тема через реестр.")
                return 'light'
        except (ImportError, FileNotFoundError, OSError) as e:
            logging.warning(f"Не удалось прочитать реестр Windows для определения темы ({e}), будет использован метод палитры.")
            return None

    def changeEvent(self, event):
        """Отслеживает изменение состояния окна (например, максимизацию)."""
        if event.type() == event.Type.WindowStateChange:
            if self.isMaximized():
                self.title_bar.maximize_button.setText("🗗") # Restore icon
            else:
                self.title_bar.maximize_button.setText("🗖") # Maximize icon
        super().changeEvent(event)

    def toggle_theme(self):
        """Переключает тему оформления (Системная -> Светлая -> Тёмная)."""
        themes = ['system', 'light', 'dark']
        try:
            current_index = themes.index(self.config_theme)
        except ValueError:
            current_index = 0 # По умолчанию системная, если что-то пошло не так
        
        next_index = (current_index + 1) % len(themes)
        self.config_theme = themes[next_index]
        
        logging.info(f"Тема оформления переключена на: {self.config_theme}")
        self.apply_theme()

    def apply_theme(self):
        """Применяет выбранную тему оформления к интерфейсу."""
        theme_map = {'system': 'Системная', 'light': 'Светлая', 'dark': 'Тёмная'}
        self.title_bar.toggle_theme_btn.setText(f"Тема: {theme_map.get(self.config_theme, 'Системная')}")

        if self.config_theme == 'system':
            # Сначала пробуем специфичный для Windows метод
            windows_theme = self.get_windows_theme()
            if windows_theme:
                self.active_theme = windows_theme
            else:
                # В качестве запасного варианта используем метод палитры (для других ОС или в случае ошибки)
                is_dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
                self.active_theme = 'dark' if is_dark else 'light'
                logging.info(f"Системная тема (метод палитры): {'тёмная' if is_dark else 'светлая'}.")
        else:
            self.active_theme = self.config_theme

        # Применяем основной стиль
        if self.active_theme == 'dark':
            self.setStyleSheet(DARK_STYLE)
        else:
            self.setStyleSheet(LIGHT_STYLE) # Применяем светлый стиль

        # Обновляем цвета, которые задаются через setStyleSheet, не вызывая полного сканирования
        if hasattr(self, 'mods_path') and self.mods_path:
            self.status_label.setStyleSheet(f"color: {THEME_COLORS[self.active_theme]['status_ok']};")
        else:
            self.status_label.setStyleSheet(f"color: {THEME_COLORS[self.active_theme]['status_error']};")
        
        # Обновляем цвет статуса загрузчика
        if self.last_loader_status:
            self.on_loader_version_check_finished(*self.last_loader_status)

        table_border_color = THEME_COLORS[self.active_theme]['table_border']
        self.table.setStyleSheet(f"""
            QHeaderView::section:horizontal {{ border-bottom: 1px solid {table_border_color}; }}
            QTableCornerButton::section {{ border-bottom: 1px solid {table_border_color}; }}
        """)

    def on_manual_mode_toggled(self, checked):
        """Обрабатывает переключение режима ввода версии/загрузчика."""
        self.game_version_combo.setEnabled(checked)
        self.loader_combo.setEnabled(checked)

        if checked:
            # В ручном режиме возвращаем полный список загрузчиков, чтобы пользователь мог выбрать любой
            current_loader = self.loader_combo.currentText()
            self.loader_combo.blockSignals(True)
            self.loader_combo.clear()
            self.loader_combo.addItems(["fabric", "forge", "neoforge", "quilt"])
            # Пытаемся восстановить выбор пользователя, если он был в списке
            if self.loader_combo.findText(current_loader) != -1:
                self.loader_combo.setCurrentText(current_loader)
            self.loader_combo.blockSignals(False)
        else:
            # Если выключаем ручной режим и папка mods выбрана, запускаем автоопределение
            if hasattr(self, 'mods_path') and self.mods_path:
                self.detect_game_versions()

        logging.info(f"Режим ручного ввода {'включен' if checked else 'выключен'}.")

    def closeEvent(self, event):
        """Вызывается при закрытии окна."""
        running_workers = []
        if self.scan_worker and self.scan_worker.isRunning():
            running_workers.append(self.scan_worker)
        if self.import_worker and self.import_worker.isRunning():
            running_workers.append(self.import_worker)
        if self.local_scan_worker and self.local_scan_worker.isRunning():
            running_workers.append(self.local_scan_worker)
        if hasattr(self, 'update_worker') and self.update_worker and self.update_worker.isRunning():
            running_workers.append(self.update_worker)
        if hasattr(self, 'loader_version_worker') and self.loader_version_worker and self.loader_version_worker.isRunning():
            running_workers.append(self.loader_version_worker)

        if running_workers:
            logging.info("Приложение закрывается, останавливаем фоновые задачи...")
            for worker in running_workers:
                if hasattr(worker, 'stop'):
                    worker.stop()
            
            for worker in running_workers:
                worker.wait()
            logging.info("Все фоновые задачи остановлены.")

        self.save_config()
        super().closeEvent(event)

    def setup_gui_logging(self):
        """Настраивает перенаправление логов в виджет QPlainTextEdit."""
        log_handler = QtLogHandler(self.log_emitter)
        # Устанавливаем форматтер, который уже используется в файловом логгере
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        log_handler.setFormatter(formatter)

        # Добавляем наш обработчик к корневому логгеру
        logging.getLogger().addHandler(log_handler)
        self.log_emitter.log_signal.connect(self.log_console.appendPlainText)

    def open_mods_folder(self):
        """Открывает папку с модами в проводнике."""
        if hasattr(self, 'mods_path') and self.mods_path and os.path.exists(self.mods_path):
            os.startfile(self.mods_path)
        else:
            CustomMessageBox(QMessageBox.Icon.Warning, "Ошибка", "Папка mods не найдена. Пожалуйста, укажите правильный путь к игре.", self).exec()

    def check_default_minecraft_path(self):
        """Проверяет и устанавливает стандартный путь к .minecraft."""
        default_path = os.path.join(os.getenv('APPDATA'), '.minecraft')
        if os.path.isdir(default_path):
            self.path_input.setText(default_path)

    def browse_folder(self):
        """Открывает диалог выбора папки."""
        path = QFileDialog.getExistingDirectory(self, "Выберите папку с игрой (где находится папка mods)")
        if path:

            self.path_input.setText(path)

    def check_mods_folder(self, path):
        """Проверяет наличие папки /mods."""
        mods_path = os.path.join(path, "mods")
        if os.path.isdir(mods_path):
            self.mods_path = mods_path
            self.status_label.setText("Папка mods найдена!")
            self.status_label.setStyleSheet(f"color: {THEME_COLORS[self.active_theme]['status_ok']};")

            self.detect_game_versions()

            # Если мы переключаем профиль и он уже есть в кэше, загружаем из памяти
            session = self.profile_cache.get_session(self.current_profile_name) if getattr(self, '_is_switching_profile', False) else None
            if session and session.is_scanned:
                last_scan_time = session.last_scanned.strftime('%H:%M:%S') if session.last_scanned else 'Неизвестно'
                logging.info(f"Загрузка списка модов из кэша для профиля: {self.current_profile_name} (сканировался: {last_scan_time})")
                self.mods_data = session.mods_data
                self.is_import_mode = False
                self.populate_table()
                
                updates_available = any(mod['status'] == "Доступно обновление" for mod in self.mods_data)
                self.set_buttons_enabled(True)
                
                self.update_selected_btn.show()
                self.update_unselected_btn.show()
                self.update_all_btn.show()
                self.install_selected_btn.hide()
                self.install_unselected_btn.hide()
                self.install_all_btn.hide()
                
                self.update_selected_btn.setEnabled(updates_available)
                self.update_unselected_btn.setEnabled(updates_available)
                self.update_all_btn.setEnabled(updates_available)
                self.action_share.setEnabled(bool(self.mods_data))
                self.action_export_json.setEnabled(bool(self.mods_data))
                self.action_export_mrpack.setEnabled(bool(self.mods_data))
                self.update_master_checkbox_state()
            else:
                self.start_local_scan()
        else:
            self.mods_path = ""
            self.status_label.setText("Папка mods не найдена")
            self.status_label.setStyleSheet(f"color: {THEME_COLORS[self.active_theme]['status_error']};")

            self.scan_btn.setEnabled(False)
            
            # ОЧИСТКА ТАБЛИЦЫ И ДАННЫХ ПРИ ОШИБКЕ ПУТИ (ИЛИ ПУСТОМ ПРОФИЛЕ)
            self.mods_data = [] # Создаем новый пустой список, чтобы не затереть старый кэш по ссылке (Исправление бага 1)
            self.row_to_dependencies_map.clear()
            self.row_to_data_map.clear()
            self.mod_index_to_rows_map.clear()
            self.table.setRowCount(0)
            self.update_master_checkbox_state()
            
    def detect_game_versions(self):
        """Автоматически определяет версии игры и загрузчики, анализируя json-файлы в папке /versions."""
        logging.info("Попытка автоопределения версий игры (углубленный анализ)...")
        versions_path = os.path.join(self.path_input.text(), 'versions')
        if not os.path.isdir(versions_path):
            logging.warning(f"Папка версий не найдена по пути: {versions_path}. Используются значения по умолчанию.")
            return

        supported_loaders = {"fabric", "neoforge", "forge", "quilt"}
        detected_versions = set()
        latest_mod_time = 0
        best_guess = None
        best_guess_id_str = None # Для хранения ID лучшей версии

        # Рекурсивно обходим все подпапки в поисках .json файлов
        for root, _, files in os.walk(versions_path):
            for filename in files:
                if not filename.endswith('.json'):
                    continue
                
                json_path = os.path.join(root, filename)
                logging.debug(f"Анализ файла версии: {json_path}")

                try:
                    with open(json_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    version_id_str = data.get('id', '')

                    game_version = None
                    # Способ 1: Ищем в зависимостях - самый надежный метод
                    libraries = data.get('libraries', [])
                    for lib in libraries:
                        lib_name = lib.get('name', '')
                        if lib_name.startswith(('net.minecraft:client:', 'com.mojang:minecraft:')):
                            parts = lib_name.split(':')
                            if len(parts) == 3 and re.fullmatch(r"1\.\d+(\.\d+)?(-\w+)?", parts[2]):
                                game_version = parts[2].split('-')[0]
                                logging.debug(f"  [Способ 1] Найдена версия игры '{game_version}' в библиотеках.")
                                break
                    
                    # Способ 2: 'inheritsFrom' - надежно для Fabric и старых версий
                    if not game_version:
                        game_version_str = data.get('inheritsFrom')
                        if game_version_str:
                            match = re.search(r"(1\.\d+(\.\d+)?)", game_version_str)
                            if match: 
                                game_version = match.group(0)
                                logging.debug(f"  [Способ 2] Найдена версия игры '{game_version}' из 'inheritsFrom'.")

                    # Способ 3: 'jar' - для очень старых версий Forge
                    if not game_version:
                        game_version_str = data.get('jar')
                        if game_version_str:
                            match = re.search(r"(1\.\d+(\.\d+)?)", game_version_str)
                            if match: 
                                game_version = match.group(0)
                                logging.debug(f"  [Способ 3] Найдена версия игры '{game_version}' из 'jar'.")

                    # Способ 4: Парсинг 'id' как последний вариант
                    if not game_version:
                        # Ищем последнюю версию в строке, т.к. она часто в конце (e.g., fabric-loader-0.15.11-1.20.1)
                        all_versions = re.findall(r"(1\.\d+(\.\d+)?)", version_id_str)
                        if all_versions:
                            game_version = all_versions[-1][0]
                            logging.debug(f"  [Способ 4] Найдена версия игры '{game_version}' из 'id'.")

                    if not game_version:
                        logging.debug("  Версия игры не найдена в этом файле, пропуск.")
                        continue

                    # Определяем загрузчик по ID версии
                    version_id_lower = version_id_str.lower()
                    loader = None
                    # Используем регулярное выражение с границами слова (\b), чтобы точно отличать "forge" от "neoforge"
                    for l in supported_loaders:
                        if re.search(r'\b' + re.escape(l) + r'\b', version_id_lower):
                            loader = l
                            logging.debug(f"  Найден загрузчик '{loader}' в 'id'.")
                            break
                    
                    if game_version and loader:
                        logging.debug(f"  => Результат: ({game_version}, {loader}).")
                        detected_versions.add((game_version, loader))
                        mod_time = os.path.getmtime(root) # Используем время модификации папки, где лежит json
                        if mod_time > latest_mod_time:
                            latest_mod_time = mod_time
                            best_guess = (game_version, loader)
                            best_guess_id_str = version_id_str
                    else:
                        logging.debug("  => Результат: Не удалось определить пару (версия, загрузчик).")

                except (json.JSONDecodeError, IOError) as e:
                    logging.warning(f"Не удалось прочитать или разобрать файл версии {json_path}: {e}")
                    continue

        # Очищаем старый статус перед новой проверкой
        self.loader_status_label.setText("")

        if detected_versions:
            all_game_versions = sorted(list(set(v[0] for v in detected_versions)), reverse=True)
            all_loaders = sorted(list(set(v[1] for v in detected_versions)))
            logging.info(f"Обнаружены версии: {all_game_versions} и загрузчики: {all_loaders}")

            self.game_version_combo.blockSignals(True)
            self.loader_combo.blockSignals(True)
            self.game_version_combo.clear()
            self.game_version_combo.addItems(all_game_versions)
            self.loader_combo.clear()
            self.loader_combo.addItems(all_loaders)
            if best_guess:
                logging.info(f"Предполагаемая последняя версия: {best_guess[0]}, загрузчик: {best_guess[1]}")
                self.game_version_combo.setCurrentText(best_guess[0])
                self.loader_combo.setCurrentText(best_guess[1])

                # --- Проверка версии загрузчика ---
                game_version, loader_type = best_guess
                current_loader_version = None
                if best_guess_id_str:
                    if loader_type == 'fabric':
                        match = re.search(r'fabric-loader-([\d\.\w-]+)-', best_guess_id_str, re.IGNORECASE)
                        if match: current_loader_version = match.group(1)
                    elif loader_type == 'quilt':
                        match = re.search(r'quilt-loader-([\d\.\w-]+)-', best_guess_id_str, re.IGNORECASE)
                        if match: current_loader_version = match.group(1)
                    elif loader_type == 'forge':
                        match = re.search(r'forge-([\d\.]+)', best_guess_id_str, re.IGNORECASE)
                        if match: current_loader_version = match.group(1)
                    elif loader_type == 'neoforge':
                        match = re.search(r'neoforge-([\d\.]+)', best_guess_id_str, re.IGNORECASE)
                        if match: current_loader_version = match.group(1)
                
                if current_loader_version:
                    logging.info(f"Определена текущая версия загрузчика: {current_loader_version}")
                    self.loader_status_label.setText("🔄 Проверка...")
                    self.loader_status_label.setToolTip("Идет проверка версии загрузчика...")
                    self.loader_status_label.setStyleSheet("") # Сбрасываем цвет на время проверки
                    self.loader_version_worker = LoaderVersionWorker(loader_type, game_version, current_loader_version)
                    self.loader_version_worker.finished.connect(self.on_loader_version_check_finished)
                    self.loader_version_worker.start()
            self.game_version_combo.blockSignals(False)
            self.loader_combo.blockSignals(False)
        else:
            logging.warning("Не удалось автоматически определить установленные версии. Используются значения по умолчанию.")

    def scan_mods(self):
        """Запускает сканирование модов в отдельном потоке."""
        if not self.mods_path:
            return
        
        # Если не в ручном режиме, переопределяем версию перед сканированием,
        # чтобы сбросить состояние после возможного импорта.
        if not self.manual_mode_checkbox.isChecked():
            self.detect_game_versions()

        self.set_buttons_enabled(False)
        self._setup_cancel_button()
        self.table.setRowCount(0) # Очищаем таблицу
        self.toggle_deps_btn.setEnabled(False)
        self.toggle_deps_btn.hide()
        
        game_version = self.game_version_combo.currentText()
        loader = self.loader_combo.currentText()

        logging.info(f"Запуск сканирования для Minecraft {game_version} ({loader}) в папке {self.mods_path}")
        self.scan_worker = ScanWorker(self.mods_path, game_version, loader, self.cache_file)
        self.scan_worker.finished.connect(self.on_scan_finished) # type: ignore
        self.scan_worker.progress.connect(self.update_scan_status)
        self.scan_worker.start()

        # Настраиваем UI для сканирования
        self.progress_label.setText("Сканирование...")
        self.progress_bar.setRange(0, 0) # Неопределенный прогресс
        self.progress_label.show()
        self.progress_bar.show()

    def start_local_scan(self):
        """Запускает быстрое локальное сканирование."""
        if not self.mods_path:
            return
        
        self.set_buttons_enabled(False)
        self._setup_cancel_button()
        self.table.setRowCount(0)
        self.toggle_deps_btn.setEnabled(False)

        self.local_scan_worker = LocalScanWorker(self.mods_path)
        self.local_scan_worker.finished.connect(self.on_local_scan_finished) # type: ignore
        self.local_scan_worker.start()

        # Настраиваем UI для сканирования
        self.progress_label.setText("Чтение локальных модов...")
        self.progress_bar.setRange(0, 0) # Неопределенный прогресс
        self.progress_label.show()
        self.progress_bar.show()

    def on_local_scan_finished(self, mods_data):
        """Обрабатывает результаты локального сканирования."""
        self.local_scan_worker = None
        self.is_import_mode = False
        self._reset_scan_button()

        self.mods_data = mods_data
        if self.current_profile_name:
            self.profile_cache.save_session(self.current_profile_name, self.mods_data)
        self.populate_table()
        
        self.statusBar().showMessage(f"Найдено {len(mods_data)} локальных модов. Нажмите 'Сканировать' для проверки обновлений.", 5000)
        
        self.progress_label.hide()
        self.progress_bar.hide()

        self.set_buttons_enabled(True)
        self.update_selected_btn.setEnabled(False)
        self.update_unselected_btn.setEnabled(False)
        self.update_all_btn.setEnabled(False)
        self.install_selected_btn.setEnabled(False)
        self.install_unselected_btn.setEnabled(False)
        self.install_all_btn.setEnabled(False)
        self.action_share.setEnabled(False)
        self.action_export_json.setEnabled(False)
        self.action_export_mrpack.setEnabled(False)
        self.update_master_checkbox_state()

    def on_scan_finished(self, mods_data):
        """Обрабатывает результаты сканирования."""
        self.scan_worker = None
        self.is_import_mode = False
        self._reset_scan_button()

        self.mods_data = mods_data
        if self.current_profile_name:
            self.profile_cache.save_session(self.current_profile_name, self.mods_data)
        updates_available = any(mod['status'] == "Доступно обновление" for mod in self.mods_data)
        logging.info(f"Сканирование завершено. Найдено {len(mods_data)} модов. Доступно обновлений: {sum(1 for m in mods_data if m['status'] == 'Доступно обновление')}")
        self.populate_table()

        self.statusBar().showMessage(f"Сканирование завершено. Найдено {len(mods_data)} модов.", 5000)
        self.set_buttons_enabled(True)

        self.progress_label.hide()
        self.progress_bar.hide()
        
        # Показываем кнопки обновления, скрываем кнопки установки
        self.update_selected_btn.show()
        self.update_unselected_btn.show()
        self.update_all_btn.show()
        self.install_selected_btn.hide()
        self.install_unselected_btn.hide()
        self.install_all_btn.hide()
        
        self.update_selected_btn.setEnabled(updates_available)
        self.update_unselected_btn.setEnabled(updates_available)
        self.update_all_btn.setEnabled(updates_available)
        self.action_share.setEnabled(bool(self.mods_data))
        self.action_export_json.setEnabled(bool(self.mods_data))
        self.action_export_mrpack.setEnabled(bool(self.mods_data))
        self.update_master_checkbox_state()

    def on_import_finished(self, mods_data):
        """Обрабатывает результаты импорта, отделяя их от локального сканирования."""
        self.import_worker = None
        self.is_import_mode = True
        self._reset_scan_button()

        self.mods_data = mods_data
        if self.current_profile_name:
            self.profile_cache.save_session(self.current_profile_name, self.mods_data)
        install_available = any(mod['status'] == "Готов к установке" for mod in self.mods_data)
        logging.info(f"Импорт завершен. Найдено {len(mods_data)} модов для установки.")
        self.populate_table()

        self.statusBar().showMessage(f"Импорт завершен. Готово к установке {len(mods_data)} модов.", 5000)
        self.set_buttons_enabled(True)

        self.progress_label.hide()
        self.progress_bar.hide()

        # Показываем кнопки установки, скрываем кнопки обновления
        self.update_selected_btn.hide()
        self.update_unselected_btn.hide()
        self.update_all_btn.hide()
        self.install_selected_btn.show()
        self.install_unselected_btn.show()
        self.install_all_btn.show()

        self.install_selected_btn.setEnabled(install_available)
        self.install_unselected_btn.setEnabled(install_available)
        self.install_all_btn.setEnabled(install_available)
        self.action_share.setEnabled(False)
        self.action_export_json.setEnabled(False)
        self.action_export_mrpack.setEnabled(False)
        self.update_master_checkbox_state()
    def _create_action_widget(self, data, mod_index=None, dep_index=None, main_mod_index=None):
        """Создает виджет с кнопками действий для строки таблицы."""
        action_widget = QWidget()
        action_widget.setStyleSheet("background-color: transparent;")
        layout = QHBoxLayout(action_widget)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(5)

        # Кнопка "Удалить"
        if data.get('filepath') and os.path.exists(data['filepath']):
            delete_button = QPushButton()
            delete_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
            delete_button.setToolTip("Удалить мод (переместить в бэкап)")
            delete_button.setProperty("mod_filepath", data['filepath'])
            delete_button.setFixedWidth(30)
            delete_button.clicked.connect(self.delete_mod)
            layout.addWidget(delete_button)

        # Кнопка "Открыть страницу"
        if data.get('project_id'):
            open_page_button = QPushButton()
            open_page_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogInfoView))
            open_page_button.setToolTip("Открыть страницу мода на Modrinth")
            open_page_button.setProperty("project_id", data['project_id'])
            open_page_button.setFixedWidth(30)
            open_page_button.clicked.connect(self.open_mod_page)
            layout.addWidget(open_page_button)

        # Кнопка "Выбрать версию"
        if data.get('project_id'):
            version_button = QPushButton()
            version_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
            version_button.setToolTip("Выбрать другую версию (Откат/Даунгрейд)")
            version_button.setProperty("project_id", data['project_id'])
            if mod_index is not None: version_button.setProperty("mod_index", mod_index)
            if dep_index is not None: version_button.setProperty("dep_index", dep_index)
            if main_mod_index is not None: version_button.setProperty("main_mod_index", main_mod_index)
            version_button.setFixedWidth(30)
            version_button.clicked.connect(self.choose_specific_version)
            layout.addWidget(version_button)

        # Кнопка "Обновить/Установить"
        if data.get('download_url'):
            action_button = QPushButton()
            action_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown))
            
            is_install = data['status'] == "Готов к установке" or not data.get('filepath')
            tooltip_verb = "Установить" if is_install else "Обновить"
            tooltip_noun = "зависимость" if dep_index is not None else "этот мод"
            action_button.setToolTip(f"{tooltip_verb} {tooltip_noun}")
            
            if mod_index is not None: action_button.setProperty("mod_index", mod_index)
            if dep_index is not None: action_button.setProperty("dep_index", dep_index)
            if main_mod_index is not None: action_button.setProperty("main_mod_index", main_mod_index)
            
            action_button.setFixedWidth(30)
            action_button.clicked.connect(self.action_button_clicked)
            layout.addWidget(action_button)

        if layout.count() > 0:
            layout.addStretch()
            return action_widget
        return None

    def _create_main_mod_row(self, mod, mod_index):
        """Создает основную строку для мода в таблице."""
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.mod_index_to_rows_map[mod_index] = row
        self.row_to_data_map[row] = mod

        # Устанавливаем кастомный номер строки для главного мода
        main_number = mod_index + 1
        header_item = QTableWidgetItem(str(main_number))
        header_item.setData(Qt.ItemDataRole.UserRole, str(main_number))
        self.table.setVerticalHeaderItem(row, header_item)

        # --- Колонка 0: Чекбокс и кнопка раскрытия ---
        checkbox = QCheckBox()
        if '_is_checked' in mod:
            checkbox.setChecked(mod['_is_checked'])
        elif mod['status'] in ["Доступно обновление", "Готов к установке"]:
            checkbox.setChecked(True)
        else:
            checkbox.setChecked(False)
            
        checkbox.stateChanged.connect(self.update_master_checkbox_state)

        cell_widget = QWidget()
        cell_widget.setStyleSheet("background-color: transparent;")
        layout = QHBoxLayout(cell_widget)
        layout.setContentsMargins(6, 0, 0, 0)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        expander_button = QPushButton()
        expander_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowRight))
        expander_button.setFixedSize(18, 18)
        
        # Удерживаем размер кнопки даже когда она скрыта, чтобы чекбокс не смещался
        sp = expander_button.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        expander_button.setSizePolicy(sp)
        expander_button.setVisible('dependencies' in mod and bool(mod['dependencies']))
        expander_button.setProperty("main_row", row)
        expander_button.setProperty("is_expanded", False)
        expander_button.clicked.connect(self.toggle_dependencies_visibility)

        layout.addWidget(checkbox)
        layout.addWidget(expander_button)
        self.table.setCellWidget(row, TableColumn.CHECKBOX, cell_widget)

        # --- Колонки 1-5: Данные мода ---
        color = None
        if mod['status'] == "Доступно обновление":
            color = QColor(THEME_COLORS[self.active_theme]['update_available'])
        elif mod['status'] == "Готов к установке":
            color = QColor(THEME_COLORS[self.active_theme]['ready_to_install'])

        items_data = [mod['name'], mod['current_version'], mod['new_version'], mod.get('new_version_date', ''), mod['status']]
        for col, data in enumerate(items_data, start=TableColumn.NAME):
            item = QTableWidgetItem(str(data))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if color:
                item.setBackground(color)
            self.table.setItem(row, col, item)

        # --- Колонка 6: Действия ---
        action_widget = self._create_action_widget(mod, mod_index=mod_index)
        if action_widget:
            self.table.setCellWidget(row, TableColumn.ACTION, action_widget)

        # --- Создание строк для зависимостей ---
        if 'dependencies' in mod:
            dependency_rows = []
            for dep_index, dep in enumerate(mod['dependencies']):
                dep_row = self._create_dependency_row(dep, mod_index, dep_index)
                dependency_rows.append(dep_row)
            self.row_to_dependencies_map[row] = dependency_rows

    def _create_dependency_row(self, dep, main_mod_index, dep_index):
        """Создает строку для зависимости в таблице."""
        dep_row = self.table.rowCount()
        self.table.insertRow(dep_row)
        self.row_to_data_map[dep_row] = dep

        # Устанавливаем иерархический номер для зависимости
        main_number = main_mod_index + 1
        dep_number = f"{main_number}.{dep_index + 1}"
        header_item = QTableWidgetItem(dep_number)
        header_item.setData(Qt.ItemDataRole.UserRole, dep_number)
        self.table.setVerticalHeaderItem(dep_row, header_item)

        # --- Колонка 0: Чекбокс ---
        dep_checkbox = QCheckBox()
        if '_is_checked' in dep:
            dep_checkbox.setChecked(dep['_is_checked'])
        elif dep.get('download_url'):
            dep_checkbox.setChecked(True)
        else:
            dep_checkbox.setChecked(False)
            
        dep_checkbox.stateChanged.connect(self.update_master_checkbox_state)
        
        dep_checkbox_widget = QWidget()
        dep_checkbox_widget.setStyleSheet("background-color: transparent;")
        dep_layout = QHBoxLayout(dep_checkbox_widget)
        # Смещаем чекбокс ровно под кнопку раскрытия
        dep_layout.setContentsMargins(28, 0, 0, 0)
        dep_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        dep_layout.addWidget(dep_checkbox)
        self.table.setCellWidget(dep_row, TableColumn.CHECKBOX, dep_checkbox_widget)

        # --- Колонки 1-5: Данные зависимости ---
        row_color = QColor("#383838") if self.active_theme == 'dark' else QColor("#f7f7f7")
        if dep.get('status') == "Доступно обновление":
            row_color = QColor(THEME_COLORS[self.active_theme]['update_available'])

        icon = "❗" if dep.get('type') == 'required' else "ℹ️"
        original_name = dep.get('name', 'N/A')
        dep_name_item = QTableWidgetItem(f"  {icon} {original_name}")
        
        # Сохраняем оригинальное имя и иконку для режима "Простой список"
        dep_name_item.setData(Qt.ItemDataRole.UserRole, original_name)
        dep_name_item.setData(Qt.ItemDataRole.UserRole + 1, icon)
        
        self.table.setItem(dep_row, TableColumn.NAME, dep_name_item)

        items_data = [dep.get('current_version', ''), dep.get('new_version', ''), dep.get('new_version_date', ''), dep.get('status', '')]
        for col, data in enumerate(items_data, start=TableColumn.CURRENT_VERSION):
            item = QTableWidgetItem(str(data))
            self.table.setItem(dep_row, col, item)

        # Применяем цвет и стили
        for col in range(TableColumn.NAME, TableColumn.ACTION):
            item = self.table.item(dep_row, col)
            item.setBackground(row_color)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        
        if dep.get('status') == "Требуется":
            self.table.item(dep_row, TableColumn.STATUS).setForeground(QColor(THEME_COLORS[self.active_theme]['status_error']))

        # --- Колонка 6: Действия ---
        action_widget = self._create_action_widget(dep, dep_index=dep_index, main_mod_index=main_mod_index)
        if action_widget:
            self.table.setCellWidget(dep_row, TableColumn.ACTION, action_widget)

        self.table.setRowHidden(dep_row, True) # Скрываем по умолчанию
        return dep_row

    def populate_table(self):
        """Заполняет таблицу данными о модах, используя вспомогательные методы."""
        self.table.setRowCount(0)
        self.row_to_dependencies_map = {}
        self.row_to_data_map = {}
        self.mod_index_to_rows_map = {}

        for mod_index, mod in enumerate(self.mods_data):
            self._create_main_mod_row(mod, mod_index)

        has_deps = bool(self.row_to_dependencies_map)
        self.toggle_deps_btn.setEnabled(has_deps)
        self.toggle_deps_btn.setVisible(has_deps)
        if not has_deps:
            self.all_dependencies_expanded = False
            self.toggle_deps_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown))
            self.toggle_deps_btn.setToolTip("Раскрыть все зависимости")
        else:
            if not self.all_dependencies_expanded:
                self.toggle_all_dependencies()

        self.filter_table() # Применяем фильтр после заполнения

    def toggle_dependencies_visibility(self):
        """Переключает видимость дочерних строк зависимостей."""
        button = self.sender()
        if not button: return

        main_row = button.property("main_row")
        if main_row is None: return

        dependency_rows = self.row_to_dependencies_map.get(main_row, [])
        if not dependency_rows: return

        is_currently_hidden = self.table.isRowHidden(dependency_rows[0])
        new_expanded_state = is_currently_hidden
        button.setProperty("is_expanded", new_expanded_state)

        if new_expanded_state:
            button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown))
        else:
            button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowRight))
        
        self.filter_table() # Перерисовываем с учетом нового состояния

    def toggle_all_dependencies(self):
        """Expands or collapses all dependency rows in the table."""
        self.all_dependencies_expanded = not self.all_dependencies_expanded
        is_expanding = self.all_dependencies_expanded

        self.table.blockSignals(True)

        for main_row, dependency_rows in self.row_to_dependencies_map.items():
            cell_widget = self.table.cellWidget(main_row, 0)
            if not cell_widget: continue
            
            # The expander button is the only QPushButton in this cell widget
            expander_button = cell_widget.findChild(QPushButton)
            if not expander_button: continue

            # Update visibility of dependency rows
            if not is_expanding:
                for dep_row in dependency_rows:
                    self.table.setRowHidden(dep_row, True)

            # Update the button icon and property
            if is_expanding:
                expander_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown))
                expander_button.setProperty("is_expanded", True)
            else:
                expander_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowRight))
                expander_button.setProperty("is_expanded", False)

        self.table.blockSignals(False)
        
        if is_expanding:
            self.filter_table() # Применяем фильтр, чтобы показать нужные зависимости

        # Update the main toggle button's icon and tooltip
        if is_expanding:
            self.toggle_deps_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowUp))
            self.toggle_deps_btn.setToolTip("Свернуть все зависимости")
        else:
            self.toggle_deps_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown))
            self.toggle_deps_btn.setToolTip("Раскрыть все зависимости")

    def get_enabled_checkboxes(self) -> list[QCheckBox]:
        """Возвращает список всех активных чекбоксов в таблице."""
        checkboxes = []
        for row in range(self.table.rowCount()):
            cell_widget = self.table.cellWidget(row, 0)
            if cell_widget and (checkbox := cell_widget.findChild(QCheckBox)) and checkbox.isEnabled():
                checkboxes.append(checkbox)
        return checkboxes


    def on_master_checkbox_changed(self, state):
        """Обрабатывает клик по главному чекбоксу для выбора/снятия всех."""
        if self.master_checkbox.checkState() == Qt.CheckState.PartiallyChecked:
            return

        is_checked = (state == Qt.CheckState.Checked.value)
        
        for cb in self.get_enabled_checkboxes():
            cb.blockSignals(True)
            cb.setChecked(is_checked)
            cb.blockSignals(False)

        # После массового изменения, нужно обновить состояние мастер-чекбокса один раз.
        self.update_master_checkbox_state()


    def update_master_checkbox_state(self):
        """Обновляет состояние главного чекбокса на основе состояния дочерних."""
        enabled_checkboxes = self.get_enabled_checkboxes()
        enabled_count = len(enabled_checkboxes)
        
        self.master_checkbox.blockSignals(True)

        if enabled_count > 0:
            checked_count = sum(1 for cb in enabled_checkboxes if cb.isChecked())
            if checked_count == 0:
                self.master_checkbox.setCheckState(Qt.CheckState.Unchecked)
            elif checked_count == enabled_count:
                self.master_checkbox.setCheckState(Qt.CheckState.Checked)
            else:
                self.master_checkbox.setCheckState(Qt.CheckState.PartiallyChecked)
            self.master_checkbox.setEnabled(True)
            self.master_checkbox.show()
        else:
            self.master_checkbox.setCheckState(Qt.CheckState.Unchecked)
            self.master_checkbox.setEnabled(False)
            self.master_checkbox.hide()

        self.master_checkbox.blockSignals(False)


    def update_selected(self):
        """Запускает обновление для выбранных модов."""
        if self.is_import_mode:
            if not self._ask_import_install_method():
                return # User cancelled or error

        mods_to_process = {} # Используем словарь для дедупликации по project_id
        for row in range(self.table.rowCount()):
            cell_widget = self.table.cellWidget(row, 0)
            checkbox = cell_widget.findChild(QCheckBox) if cell_widget else None
            if checkbox and checkbox.isChecked():
                mod_data = self.row_to_data_map.get(row)
                if mod_data and mod_data.get('project_id'):
                    project_id = mod_data['project_id']
                    if project_id not in mods_to_process:
                        mods_to_process[project_id] = {'data': mod_data, 'row': row}
        
        if mods_to_process:
            unique_mods = [item['data'] for item in mods_to_process.values()]
            unique_rows = [item['row'] for item in mods_to_process.values()]
            logging.info(f"Запуск обновления для {len(unique_mods)} выбранных уникальных модов.")
            self.start_update_worker(unique_mods, unique_rows)
        else:
            logging.info("Нет выбранных модов для обновления.")
            msg_box = CustomMessageBox(QMessageBox.Icon.Information, "Нет выбора", "Не выбрано ни одного мода для обновления или установки.", self)
            msg_box.addButton("ОК", QMessageBox.ButtonRole.AcceptRole)
            msg_box.exec()


    def update_unselected(self):
        """Запускает обновление для всех модов с доступным обновлением, КРОМЕ выбранных."""
        if self.is_import_mode:
            if not self._ask_import_install_method():
                return

        mods_to_process = {}
        for row in range(self.table.rowCount()):
            cell_widget = self.table.cellWidget(row, TableColumn.CHECKBOX)
            checkbox = cell_widget.findChild(QCheckBox) if cell_widget else None
            
            mod_data = self.row_to_data_map.get(row)
            if mod_data and mod_data.get('status') in ["Доступно обновление", "Готов к установке"]:
                # Инвертированная логика: добавляем те, где НЕТ галочки
                if checkbox and not checkbox.isChecked():
                    project_id = mod_data.get('project_id')
                    if project_id and project_id not in mods_to_process:
                        mods_to_process[project_id] = {'data': mod_data, 'row': row}
        
        if mods_to_process:
            unique_mods = [item['data'] for item in mods_to_process.values()]
            unique_rows = [item['row'] for item in mods_to_process.values()]
            logging.info(f"Запуск инвертированного обновления для {len(unique_mods)} модов.")
            self.start_update_worker(unique_mods, unique_rows)
        else:
            logging.info("Нет невыбранных модов для обновления.")
            msg_box = CustomMessageBox(QMessageBox.Icon.Information, "Нет выбора", "Не найдено невыделенных модов для обновления.", self)
            msg_box.addButton("ОК", QMessageBox.ButtonRole.AcceptRole)
            msg_box.exec()

    def invert_selection(self):
        """Инвертирует выделение всех чекбоксов в таблице."""
        for cb in self.get_enabled_checkboxes():
            cb.blockSignals(True)
            cb.setChecked(not cb.isChecked())
            cb.blockSignals(False)
        self.update_master_checkbox_state()

    def force_refresh_list(self):
        """Принудительно пересканирует папку, обходя кэш."""
        if self.mods_path:
            logging.info("Принудительное обновление списка модов (игнорирование кэша)...")
            if self.current_profile_name:
                self.profile_cache.clear_session(self.current_profile_name)
            self.start_local_scan()

    def delete_selected_mods(self):
        """Удаляет выбранные моды физически и из таблицы."""
        mods_to_delete = []
        for row in range(self.table.rowCount()):
            cell_widget = self.table.cellWidget(row, TableColumn.CHECKBOX)
            if cell_widget and (checkbox := cell_widget.findChild(QCheckBox)):
                if checkbox.isChecked():
                    mod_data = self.row_to_data_map.get(row)
                    if mod_data and mod_data not in mods_to_delete:
                        mods_to_delete.append(mod_data)

        if not mods_to_delete:
            CustomMessageBox(QMessageBox.Icon.Information, "Нет выбора", "Нет выбранных модов для удаления.", self).exec()
            return

        msg_box = CustomMessageBox(QMessageBox.Icon.Question, "Подтверждение", f"Вы уверены, что хотите удалить {len(mods_to_delete)} модов?\nОни будут перемещены в папку бэкапов.", self)
        yes_btn = msg_box.addButton("Да", QMessageBox.ButtonRole.YesRole)
        no_btn = msg_box.addButton("Нет", QMessageBox.ButtonRole.NoRole)
        msg_box.exec()

        if msg_box.clickedButton() == yes_btn:
            deleted_count = 0
            for mod_data in mods_to_delete:
                filepath = mod_data.get('filepath')
                if filepath and os.path.exists(filepath):
                    try:
                        backup_and_remove_mod(filepath)
                        deleted_count += 1
                    except Exception as e:
                        logging.error(f"Не удалось удалить {filepath}: {e}")
            
            # Удаляем из mods_data
            for mod_data in mods_to_delete:
                if mod_data in self.mods_data:
                    self.mods_data.remove(mod_data)
                else:
                    for m in self.mods_data:
                        if 'dependencies' in m and mod_data in m['dependencies']:
                            m['dependencies'].remove(mod_data)
                            
            if self.current_profile_name:
                self.profile_cache.save_session(self.current_profile_name, self.mods_data)
                
            self.populate_table()
            self.statusBar().showMessage(f"Успешно удалено {deleted_count} файлов.", 5000)

    def update_all(self):
        """Запускает обновление для всех модов, где оно доступно."""
        if self.is_import_mode:
            if not self._ask_import_install_method():
                return # User cancelled or error

        mods_to_process = {} # Используем словарь для дедупликации по project_id
        for row, mod_data in self.row_to_data_map.items():
            if mod_data.get('status') in ["Доступно обновление", "Готов к установке"]:
                project_id = mod_data.get('project_id')
                if project_id and project_id not in mods_to_process:
                    mods_to_process[project_id] = {'data': mod_data, 'row': row}

        if mods_to_process:
            unique_mods = [item['data'] for item in mods_to_process.values()]
            unique_rows = [item['row'] for item in mods_to_process.values()]
            logging.info(f"Запуск обновления для всех {len(unique_mods)} доступных уникальных модов.")
            self.start_update_worker(unique_mods, unique_rows)
        else:
            logging.info("Нет доступных модов для обновления.")


    def start_update_worker(self, mods_to_update, rows):
        """Инициализирует и запускает поток обновления."""
        self.set_buttons_enabled(False)
        # Создаем словарь для быстрого доступа к строке по данным мода
        self.row_map = {mod['project_id']: row for row, mod in zip(rows, mods_to_update)}
        
        # Настраиваем прогресс-бар
        self.progress_bar.setMaximum(len(mods_to_update))
        self.progress_bar.setValue(0)
        self.progress_label.setText(f"Обновлено 0 / {len(mods_to_update)}")
        self.progress_bar.show()
        self.progress_label.show()

        self.update_worker = UpdateWorker(mods_to_update, self.mods_path)
        self.update_worker.progress.connect(self.update_mod_status) # type: ignore
        self.update_worker.download_progress.connect(self.update_download_progress)
        self.update_worker.finished.connect(self.on_update_finished) # type: ignore
        self.update_worker.start()


    def update_mod_status(self, mod_index, status):
        """Обновляет статус мода во всех строках таблицы, относящихся к нему."""
        mod_data = self.update_worker.mods_to_update[mod_index]
        project_id_to_update = mod_data.get('project_id')
        if not project_id_to_update:
            return

        # Находим все строки, которые относятся к этому project_id
        rows_to_update = [row for row, data in self.row_to_data_map.items() if data.get('project_id') == project_id_to_update]
        if not rows_to_update:
            return

        # Обновляем статус в основной модели данных (в первом найденном экземпляре)
        self.row_to_data_map[rows_to_update[0]]['status'] = status

        # --- Логика обновления UI для каждой строки ---
        
        # 1. Обработка начала скачивания (показываем QProgressBar)
        if status == "Скачивание...":
            for row in rows_to_update:
                progress_bar = QProgressBar()
                progress_bar.setRange(0, 0) # Неопределенный до первого обновления
                progress_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
                progress_bar.setFormat("Скачивание...")
                self.table.setCellWidget(row, TableColumn.STATUS, progress_bar)
            return # Выходим, остальная логика для финальных статусов

        # 2. Обработка финальных статусов
        
        # Обновляем общий прогресс-бар только один раз за мод, на финальном статусе
        if status not in ("Завершение...", "Отменено"):
            current_value = self.progress_bar.value() + 1
            self.progress_bar.setValue(current_value)
            self.progress_label.setText(f"Обновлено {current_value} / {self.progress_bar.maximum()}")

        # Определяем цвет
        color = None
        is_success = status in ("Обновлено!", "Установлено!")
        if is_success:
            color = QColor(THEME_COLORS[self.active_theme]['updated'])
        elif "Ошибка" in status:
            color = QColor(THEME_COLORS[self.active_theme]['update_error'])
        elif status == "Завершение...":
            color = QColor(THEME_COLORS[self.active_theme]['downloading'])

        # Обновляем все найденные строки
        for row in rows_to_update:
            # Удаляем QProgressBar и ставим текстовый статус
            self.table.removeCellWidget(row, TableColumn.STATUS)
            status_item = QTableWidgetItem(status)
            if color:
                status_item.setBackground(color)
            self.table.setItem(row, TableColumn.STATUS, status_item)

            # Если успех, обновляем всю информацию в строке
            if is_success:
                # Обновляем данные в модели для этой конкретной строки
                row_data = self.row_to_data_map[row]
                new_version = mod_data['new_version']
                new_filename = mod_data['new_filename']
                row_data['current_version'] = new_version
                row_data['new_version'] = ""
                row_data['new_version_date'] = ""
                row_data['download_url'] = None
                row_data['filepath'] = os.path.join(self.mods_path, new_filename) if new_filename else row_data.get('filepath')
                row_data['new_filename'] = None
                
                self.table.setItem(row, TableColumn.CURRENT_VERSION, QTableWidgetItem(new_version))
                self.table.setItem(row, TableColumn.NEW_VERSION, QTableWidgetItem(""))
                self.table.setItem(row, TableColumn.RELEASE_DATE, QTableWidgetItem(""))
                
                # Отключаем чекбокс
                checkbox_widget = self.table.cellWidget(row, TableColumn.CHECKBOX)
                if checkbox_widget and (checkbox := checkbox_widget.findChild(QCheckBox)):
                    checkbox.setChecked(False)
                    checkbox.setEnabled(False)
                
                # Обновляем виджет "Действие"
                action_widget = self._create_action_widget(row_data) # Пересоздаем виджет с актуальными данными
                if action_widget:
                    self.table.setCellWidget(row, TableColumn.ACTION, action_widget)
                else:
                    self.table.removeCellWidget(row, TableColumn.ACTION)

            # Применяем цвет ко всей строке
            if color:
                for col in range(self.table.columnCount()):
                    item = self.table.item(row, col)
                    if item:
                        item.setBackground(color)
                    else: # Для виджетов
                        widget = self.table.cellWidget(row, col)
                        if widget:
                            widget.setAutoFillBackground(True)
                            p = widget.palette()
                            p.setColor(QPalette.ColorRole.Window, color)
                            widget.setPalette(p)

    def update_download_progress(self, mod_index, downloaded, total):
        """Обновляет индивидуальный прогресс-бар для скачиваемого мода во всех его строках."""
        mod_data = self.update_worker.mods_to_update[mod_index]
        project_id_to_update = mod_data.get('project_id')
        if not project_id_to_update:
            return

        rows_to_update = [row for row, data in self.row_to_data_map.items() if data.get('project_id') == project_id_to_update]

        for row in rows_to_update:
            widget = self.table.cellWidget(row, TableColumn.STATUS)
            if isinstance(widget, QProgressBar):
                progress_bar = widget
                if progress_bar.maximum() == 0:
                    progress_bar.setRange(0, total)
                
                progress_bar.setValue(downloaded)
                
                downloaded_mb = downloaded / (1024 * 1024)
                total_mb = total / (1024 * 1024)
                progress_bar.setFormat(f"{downloaded_mb:.1f} / {total_mb:.1f} MB")

    def on_update_finished(self):
        """Вызывается после завершения всех обновлений."""
        num_updated = len(self.update_worker.mods_to_update) if self.update_worker else 0

        self.progress_bar.hide()
        self.progress_label.hide()

        self.statusBar().showMessage("Обновление завершено! Перезапустите сканирование для проверки.", 10000)
        
        if self.current_profile_name:
            self.profile_cache.save_session(self.current_profile_name, self.mods_data)
        
        # Включаем общие кнопки управления (Сканировать, Обзор)
        self.scan_btn.setEnabled(True)
        self.browse_btn.setEnabled(True)
        # Переоцениваем, есть ли еще доступные обновления после этой партии
        updates_still_available = any(mod['status'] == "Доступно обновление" for mod in self.mods_data)
        
        # Устанавливаем состояние кнопок обновления на основе фактической доступности
        self.update_selected_btn.setEnabled(updates_still_available)
        self.update_all_btn.setEnabled(updates_still_available)

        # Также нужно обработать кнопки установки
        installs_still_available = any(mod['status'] == "Готов к установке" for mod in self.mods_data)
        self.install_selected_btn.setEnabled(installs_still_available)
        self.install_all_btn.setEnabled(installs_still_available)

        self.update_master_checkbox_state()
        
        # Показываем уведомление только если было обновлено больше одного мода
        if num_updated > 1:
            msg_box = CustomMessageBox(QMessageBox.Icon.Information, "Готово", "Обновление модов завершено. Рекомендуется перезапустить сканирование.", self)
            msg_box.addButton("ОК", QMessageBox.ButtonRole.AcceptRole)
            msg_box.exec()

    def set_buttons_enabled(self, enabled):
        """Включает/выключает кнопки управления."""
        self.scan_btn.setEnabled(enabled and bool(self.mods_path))
        self.browse_btn.setEnabled(enabled)
        self.update_selected_btn.setEnabled(enabled)
        self.update_unselected_btn.setEnabled(enabled)
        self.update_all_btn.setEnabled(enabled)
        self.install_selected_btn.setEnabled(enabled)
        self.install_unselected_btn.setEnabled(enabled)
        self.install_all_btn.setEnabled(enabled)
        
        self.import_export_btn.setEnabled(enabled)
        self.action_import.setEnabled(enabled)
        self.action_sync.setEnabled(enabled)
        self.action_share.setEnabled(enabled and bool(self.mods_data))
        self.action_export_json.setEnabled(enabled and bool(self.mods_data))
        self.action_export_mrpack.setEnabled(enabled and bool(self.mods_data))
        
        if hasattr(self, 'delete_selected_btn'):
            self.delete_selected_btn.setEnabled(enabled and bool(self.mods_data))
            self.invert_selection_btn.setEnabled(enabled and bool(self.mods_data))
            self.refresh_list_btn.setEnabled(enabled and bool(self.mods_path))
        # Состояние master_checkbox обновляется отдельно в on_scan_finished и on_update_finished

    def import_mod_list(self):
        """Открывает диалог для импорта списка модов и запускает обработку."""
        dialog = ImportDialog(self)
        if dialog.exec():
            share_string = dialog.get_share_string().strip()
            if not share_string:
                return

            try:
                decoded_bytes = base64.urlsafe_b64decode(share_string)
                decompressed_json = zlib.decompress(decoded_bytes).decode('utf-8')
                share_data = json.loads(decompressed_json)
                self._process_imported_data(share_data)
            except (ValueError, TypeError, zlib.error, json.JSONDecodeError) as e:
                logging.error(f"Ошибка при импорте списка модов: {e}")
                msg_box = CustomMessageBox(QMessageBox.Icon.Critical, "Ошибка импорта", f"Не удалось прочитать строку: {e}", self)
                msg_box.addButton("ОК", QMessageBox.ButtonRole.AcceptRole)
                msg_box.exec()

    def sync_from_url(self):
        """Загружает JSON конфигурацию сборки по URL."""
        dialog = SyncDialog(self)
        if dialog.exec():
            url = dialog.get_url()
            if not url: return
            try:
                import requests
                self.statusBar().showMessage("Загрузка данных с сервера...")
                QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                response = requests.get(url, timeout=15)
                response.raise_for_status()
                share_data = response.json()
                QApplication.restoreOverrideCursor()
                self._process_imported_data(share_data)
            except Exception as e:
                QApplication.restoreOverrideCursor()
                logging.error(f"Ошибка при синхронизации по URL: {e}")
                msg_box = CustomMessageBox(QMessageBox.Icon.Critical, "Ошибка", f"Не удалось загрузить данные:\n{e}", self)
                msg_box.addButton("ОК", QMessageBox.ButtonRole.AcceptRole)
                msg_box.exec()

    def _process_imported_data(self, share_data):
        try:
            if 'v' not in share_data or 'mc' not in share_data or 'l' not in share_data:
                raise ValueError("Неверный формат данных.")

            game_version = share_data['mc']
            loader = share_data['l']
            
            v = share_data['v']
            use_exact = False
            if v == 1:
                target_mods = share_data['p']
                project_ids = target_mods
                strict = False
            elif v == 2:
                target_mods = share_data.get('mods', {})
                project_ids = list(target_mods.keys())
                strict = share_data.get('strict', False)
            else:
                raise ValueError("Неподдерживаемая версия формата данных.")

            if v >= 2:
                strict_text = "⚠️ Сборка содержит ЖЕСТКУЮ привязку версий." if strict else "ℹ️ Сборка не содержит жесткую привязку."
                msg_box = CustomMessageBox(QMessageBox.Icon.Question, 'Настройки сборки',
                                           f"Версия Minecraft: {game_version} ({loader})\n"
                                           f"Модов в списке: {len(project_ids)}\n\n{strict_text}\n\n"
                                           "Как вы хотите установить моды?", self)
                exact_btn = msg_box.addButton("Точные версии", QMessageBox.ButtonRole.YesRole)
                latest_btn = msg_box.addButton("Самые новые", QMessageBox.ButtonRole.NoRole)
                cancel_btn = msg_box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
                msg_box.setDefaultButton(exact_btn if strict else latest_btn)
            else:
                msg_box = CustomMessageBox(QMessageBox.Icon.Question, 'Подтверждение',
                                           f"Импортировать {len(project_ids)} модов для MC {game_version} ({loader})?", self)
                yes_button = msg_box.addButton("Да", QMessageBox.ButtonRole.YesRole)
                cancel_btn = msg_box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
                msg_box.setDefaultButton(yes_button)
                
            msg_box.exec()

            if msg_box.clickedButton() == cancel_btn or msg_box.clickedButton() is None: return
            if v >= 2: use_exact = (msg_box.clickedButton() == exact_btn)

            self.game_version_combo.setCurrentText(game_version)
            if self.loader_combo.findText(loader) == -1:
                self.loader_combo.addItem(loader)
            self.loader_combo.setCurrentText(loader)

            self.set_buttons_enabled(False)
            self._setup_cancel_button()
            self.table.setRowCount(0)
            self.toggle_deps_btn.setEnabled(False)
            self.toggle_deps_btn.hide()

            self.import_worker = ImportWorker(target_mods, game_version, loader, use_exact)
            self.import_worker.finished.connect(self.on_import_finished) # type: ignore
            self.import_worker.progress.connect(self.update_scan_status)
            self.import_worker.start()

            self.progress_label.setText("Обработка сборки...")
            self.progress_bar.setRange(0, 0)
            self.progress_label.show()
            self.progress_bar.show()

        except Exception as e:
            logging.error(f"Ошибка при разборе данных: {e}")
            CustomMessageBox(QMessageBox.Icon.Critical, "Ошибка", f"Не удалось прочитать данные:\n{e}", self).exec()

    def share_mod_list(self):
        """Собирает список project_id, кодирует его и показывает в диалоге."""
        if not self.mods_data:
            msg_box = CustomMessageBox(QMessageBox.Icon.Warning, "Нечего отправлять", "Список модов пуст. Сначала выполните сканирование.", self)
            msg_box.addButton("ОК", QMessageBox.ButtonRole.AcceptRole)
            msg_box.exec()
            return

        project_ids = {mod['project_id'] for mod in self.mods_data if mod.get('project_id')}
        if not project_ids:
            msg_box = CustomMessageBox(QMessageBox.Icon.Warning, "Нет данных", "Не удалось найти ID проектов для модов.", self)
            msg_box.addButton("ОК", QMessageBox.ButtonRole.AcceptRole)
            msg_box.exec()
            return

        # Настройка экспорта (жесткая привязка)
        msg_box = CustomMessageBox(QMessageBox.Icon.Question,
                                   "Настройки экспорта",
                                   "Вы хотите жестко привязать сборку к текущим версиям модов?\n\n"
                                   "Да - получателю будут предложены точно такие же версии.\n"
                                   "Нет - получателю будут предложены самые новые версии.",
                                   self)
        yes_btn = msg_box.addButton("Точные версии", QMessageBox.ButtonRole.YesRole)
        no_btn = msg_box.addButton("Всегда новые", QMessageBox.ButtonRole.NoRole)
        cancel_btn = msg_box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        msg_box.exec()

        if msg_box.clickedButton() == cancel_btn or msg_box.clickedButton() is None:
            return

        strict = (msg_box.clickedButton() == yes_btn)

        game_version = self.game_version_combo.currentText()
        loader = self.loader_combo.currentText()

        mods_dict = {}
        for mod in self.mods_data:
            pid = mod.get('project_id')
            if pid:
                mods_dict[pid] = mod.get('current_version', '')

        share_data = {"v": 2, "mc": game_version, "l": loader, "strict": strict, "mods": mods_dict}
        
        json_string = json.dumps(share_data, separators=(',', ':'))
        compressed = zlib.compress(json_string.encode('utf-8'), level=9)
        encoded_string = base64.urlsafe_b64encode(compressed).decode('ascii')

        logging.info(f"Сгенерирована строка для обмена: {encoded_string}")
        dialog = ShareDialog(encoded_string, self)
        dialog.exec()

    def export_server_json(self):
        """Экспортирует список модов в JSON файл для синхронизации по URL."""
        if not self.mods_data:
            return

        msg_box = CustomMessageBox(QMessageBox.Icon.Question,
                                   "Настройки экспорта",
                                   "Вы хотите жестко привязать сборку к текущим версиям модов?\n\n"
                                   "Да - клиенты скачают точно такие же версии.\n"
                                   "Нет - клиенты скачают самые новые версии.",
                                   self)
        yes_btn = msg_box.addButton("Точные версии", QMessageBox.ButtonRole.YesRole)
        no_btn = msg_box.addButton("Всегда новые", QMessageBox.ButtonRole.NoRole)
        cancel_btn = msg_box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        msg_box.exec()

        if msg_box.clickedButton() == cancel_btn or msg_box.clickedButton() is None:
            return

        strict = (msg_box.clickedButton() == yes_btn)
        game_version = self.game_version_combo.currentText()
        loader = self.loader_combo.currentText()

        mods_dict = {}
        for mod in self.mods_data:
            pid = mod.get('project_id')
            if pid: mods_dict[pid] = mod.get('current_version', '')

        share_data = {"v": 2, "mc": game_version, "l": loader, "strict": strict, "mods": mods_dict}
        
        file_path, _ = QFileDialog.getSaveFileName(self, "Сохранить JSON для сервера", "modpack.json", "JSON Files (*.json)")
        if file_path:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(share_data, f, indent=4)
            self.statusBar().showMessage(f"Файл сохранен: {os.path.basename(file_path)}", 5000)

    def export_mrpack(self):
        """Экспортирует установленные моды в формат Modrinth (.mrpack)."""
        if not self.mods_data:
            return

        game_version = self.game_version_combo.currentText()
        loader = self.loader_combo.currentText()
        
        loader_version = "latest"
        if self.last_loader_status:
            match = re.search(r'([\d\.]+)', self.last_loader_status[0])
            if match: loader_version = match.group(1)
            
        files_list = []
        added_project_ids = set()
        
        def _add_to_files_list(mod_dict):
            info = mod_dict.get('installed_info')
            pid = mod_dict.get('project_id')
            if not info or not pid or pid in added_project_ids: return
            
            primary_file = next((f for f in info.get('files', []) if f.get('primary')), None)
            if not primary_file and info.get('files'):
                primary_file = info['files'][0]
            
            if not primary_file: return

            files_list.append({
                "path": f"mods/{primary_file['filename']}",
                "hashes": {
                    "sha1": primary_file['hashes'].get('sha1', ''),
                    "sha512": primary_file['hashes'].get('sha512', '')
                },
                "env": {"client": "required", "server": "required"},
                "downloads": [primary_file['url']],
                "fileSize": primary_file.get('size', 0)
            })
            added_project_ids.add(pid)

        for mod in self.mods_data:
            _add_to_files_list(mod)
            for dep in mod.get('dependencies', []):
                if dep.get('installed'):
                    _add_to_files_list(dep)

        if not files_list:
            msg_box = CustomMessageBox(QMessageBox.Icon.Warning, "Ошибка", "Не найдено данных для экспорта. Для создания .mrpack моды должны быть проверены через Modrinth.", self)
            msg_box.addButton("ОК", QMessageBox.ButtonRole.AcceptRole)
            msg_box.exec()
            return

        loader_key = f"{loader}-loader" if loader in ['fabric', 'quilt'] else loader
        index_json = {
            "formatVersion": 1,
            "game": "minecraft",
            "versionId": f"Modpack {game_version}",
            "name": f"Modpack {game_version}",
            "dependencies": {
                "minecraft": game_version,
                loader_key: loader_version
            },
            "files": files_list
        }

        file_path, _ = QFileDialog.getSaveFileName(self, "Сохранить .mrpack", "", "Modrinth Modpack (*.mrpack)")
        if file_path:
            try:
                with zipfile.ZipFile(file_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    zf.writestr("modrinth.index.json", json.dumps(index_json, indent=4))
                self.statusBar().showMessage(f"Сборка экспортирована в {os.path.basename(file_path)}!", 5000)
                msg_box = CustomMessageBox(QMessageBox.Icon.Information, "Успех", "Сборка успешно экспортирована в формат .mrpack!\nТеперь её можно импортировать в Prism Launcher или Modrinth App.", self)
                msg_box.addButton("ОК", QMessageBox.ButtonRole.AcceptRole)
                msg_box.exec()
            except Exception as e:
                logging.error(f"Ошибка при экспорте mrpack: {e}")
                msg_box = CustomMessageBox(QMessageBox.Icon.Critical, "Ошибка", f"Не удалось сохранить файл:\n{e}", self)
                msg_box.addButton("ОК", QMessageBox.ButtonRole.AcceptRole)
                msg_box.exec()

    def open_mod_page(self):
        """Открывает страницу мода на Modrinth в браузере."""
        button = self.sender()
        if not button:
            return

        project_id = button.property("project_id")
        if project_id:
            url = f"https://modrinth.com/mod/{project_id}"
            logging.info(f"Открытие страницы мода: {url}")
            webbrowser.open(url)
        else:
            logging.warning("Кнопка 'открыть страницу' нажата, но project_id не найден.")

    def action_button_clicked(self):
        """Обрабатывает нажатие кнопки действия (обновить/установить) в строке."""
        button = self.sender()
        if not button:
            return

        mod_index = button.property("mod_index")
        dep_index = button.property("dep_index")
        main_mod_index = button.property("main_mod_index")

        mod_to_process = None
        table_row = -1

        if dep_index is not None and main_mod_index is not None:
            # Это зависимость
            mod_to_process = self.mods_data[main_mod_index]['dependencies'][dep_index]
            main_mod_row = self.mod_index_to_rows_map.get(main_mod_index)
            if main_mod_row is not None:
                dep_rows = self.row_to_dependencies_map.get(main_mod_row, [])
                if dep_index < len(dep_rows):
                    table_row = dep_rows[dep_index]

        elif mod_index is not None:
            # Это основной мод
            if 0 <= mod_index < len(self.mods_data):
                mod_to_process = self.mods_data[mod_index]
                table_row = self.mod_index_to_rows_map.get(mod_index, -1)

        if mod_to_process and table_row != -1:
            logging.info(f"Запуск индивидуальной операции для мода '{mod_to_process['name']}' в строке {table_row}.")
            self.start_update_worker([mod_to_process], [table_row])
        else:
            logging.error(f"Кнопка действия нажата, но не удалось найти мод или строку. main_mod_index: {main_mod_index}, dep_index: {dep_index}, mod_index: {mod_index}")

    def choose_specific_version(self):
        """Открывает диалог для выбора конкретной версии мода (даунгрейд/апгрейд)."""
        button = self.sender()
        if not button: return

        project_id = button.property("project_id")
        mod_index = button.property("mod_index")
        dep_index = button.property("dep_index")
        main_mod_index = button.property("main_mod_index")

        game_version = self.game_version_combo.currentText()
        loader = self.loader_combo.currentText()

        try:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            versions = get_all_versions(project_id, game_version, loader)
            QApplication.restoreOverrideCursor()
            
            if not versions:
                CustomMessageBox(QMessageBox.Icon.Warning, "Пусто", "Не найдено доступных версий.", self).exec()
                return

            dialog = VersionPickerDialog(versions, self)
            if dialog.exec():
                self._apply_specific_version(dialog.selected_version, mod_index, dep_index, main_mod_index)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            CustomMessageBox(QMessageBox.Icon.Critical, "Ошибка", f"Не удалось получить список версий:\n{e}", self).exec()

    def _apply_specific_version(self, version_info, mod_index, dep_index, main_mod_index):
        """Применяет выбранную версию к таблице."""
        mod_to_process = None
        if dep_index is not None and main_mod_index is not None:
            mod_to_process = self.mods_data[main_mod_index]['dependencies'][dep_index]
        elif mod_index is not None:
            mod_to_process = self.mods_data[mod_index]
        
        if mod_to_process:
            primary_file = next((f for f in version_info.get('files', []) if f.get('primary')), None)
            if not primary_file and version_info.get('files'):
                primary_file = version_info['files'][0]
            
            if primary_file:
                mod_to_process['new_version'] = version_info['version_number']
                if date_str := version_info.get('date_published', ''): mod_to_process['new_version_date'] = date_str.split('T')[0]
                mod_to_process['download_url'] = primary_file['url']
                mod_to_process['new_filename'] = primary_file['filename']
                mod_to_process['status'] = "Готов к установке"
                
                self.populate_table()
                self.update_master_checkbox_state()
                if self.current_profile_name:
                    self.profile_cache.save_session(self.current_profile_name, self.mods_data)

    def _ask_import_install_method(self) -> bool:
        """
        Asks the user for the installation method during import.
        Returns True if installation should proceed, False if cancelled.
        """
        install_method_box = CustomMessageBox(QMessageBox.Icon.Question,
                                           'Способ установки',
                                           "", # Текст устанавливается ниже
                                           self)
        install_method_box.text_label.setTextFormat(Qt.TextFormat.RichText)
        install_method_box.text_label.setText("<b>Заменить все (рекомендуется):</b><br>Все текущие моды будут перемещены в бэкап, после чего будет установлена новая сборка.<br><br>"
                                               "<b>Объединить с текущими:</b><br>Моды из списка будут добавлены или обновлены. Остальные моды в папке останутся нетронутыми.")
        
        replace_button = install_method_box.addButton("Заменить все", QMessageBox.ButtonRole.YesRole)
        merge_button = install_method_box.addButton("Объединить", QMessageBox.ButtonRole.NoRole)
        cancel_button = install_method_box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        install_method_box.setDefaultButton(replace_button)
        
        install_method_box.exec()
        
        clicked_button = install_method_box.clickedButton()

        if clicked_button == cancel_button or clicked_button is None:
            logging.info("Пользователь отменил выбор способа установки.")
            return False

        if clicked_button == replace_button:
            logging.info("Выбран способ 'Заменить все'. Создание бэкапа папки mods...")
            if not self.mods_path or not os.path.isdir(self.mods_path):
                msg_box = CustomMessageBox(QMessageBox.Icon.Critical, "Ошибка", "Путь к папке mods недействителен.", self)
                msg_box.addButton("ОК", QMessageBox.ButtonRole.AcceptRole)
                msg_box.exec()
                return False

            try:
                # Главная папка для бэкапов внутри папки mods
                main_backup_dir = os.path.join(self.mods_path, "ModUpdater_Backups")
                # Подпапка для бэкапов сборок
                modpack_backup_dir = os.path.join(main_backup_dir, "modpack_backups")
                os.makedirs(modpack_backup_dir, exist_ok=True)

                timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                destination = os.path.join(modpack_backup_dir, f"modpack_backup_{timestamp}")
                os.makedirs(destination)
                
                # Перемещаем все содержимое папки mods (кроме самой папки бэкапов)
                for item_name in os.listdir(self.mods_path):
                    source_path = os.path.join(self.mods_path, item_name)
                    if os.path.normpath(source_path) == os.path.normpath(main_backup_dir):
                        continue
                    shutil.move(source_path, destination)

                logging.info(f"Все моды и файлы перемещены в папку бэкапа: {destination}.")
            except Exception as e:
                logging.error(f"Ошибка при замене папки mods: {e}")
                error_box = CustomMessageBox(QMessageBox.Icon.Critical, "Ошибка бэкапа", f"Не удалось заменить папку mods: {e}", self)
                error_box.addButton("ОК", QMessageBox.ButtonRole.AcceptRole)
                error_box.exec()
                return False
        
        elif clicked_button == merge_button:
            logging.info("Выбран способ 'Объединить'.")
        
        self.is_import_mode = False 
        return True

    def delete_mod(self):
        """Удаляет мод, связанный с нажатой кнопкой."""
        button = self.sender()
        if not button:
            return

        mod_filepath = button.property("mod_filepath")
        if not mod_filepath:
            logging.warning("Кнопка удаления нажата, но свойство 'mod_filepath' не найдено.")
            return

        # Находим мод и его индекс в self.mods_data
        target_row = -1
        target_mod_data = None
        for i, mod in enumerate(self.mods_data):
            # Сравниваем нормализованные пути на всякий случай
            if mod.get('filepath') and os.path.normpath(mod['filepath']) == os.path.normpath(mod_filepath):
                target_row = i
                target_mod_data = mod
                break

        if target_row == -1:
            logging.error(f"Не удалось найти мод с путем {mod_filepath} для удаления.")
            msg_box = CustomMessageBox(QMessageBox.Icon.Warning, "Ошибка", "Не удалось найти данные для этого мода. Возможно, список уже устарел.", self)
            msg_box.addButton("ОК", QMessageBox.ButtonRole.AcceptRole)
            msg_box.exec()
            return

        mod_name = target_mod_data.get('name', os.path.basename(mod_filepath))

        # Диалог подтверждения
        msg_box = CustomMessageBox(QMessageBox.Icon.Question,
                                   'Подтверждение удаления',
                                   f"Вы уверены, что хотите удалить мод '{mod_name}'?\n\n"
                                   f"Файл будет перемещен в папку 'mod_backups'.",
                                   self)
        yes_button = msg_box.addButton("Да, удалить", QMessageBox.ButtonRole.YesRole)
        no_button = msg_box.addButton("Нет", QMessageBox.ButtonRole.NoRole)
        msg_box.setDefaultButton(no_button)
        msg_box.exec()

        if msg_box.result() == QDialog.DialogCode.Accepted:
            try:
                logging.info(f"Пользователь подтвердил удаление мода '{mod_name}' (файл: {os.path.basename(mod_filepath)}).")
                backup_and_remove_mod(mod_filepath)
                
                del self.mods_data[target_row] # Удаляем из модели данных
                self.populate_table() # Полностью перерисовываем таблицу

                logging.info(f"Мод '{mod_name}' успешно удален.")
                self.statusBar().showMessage(f"Мод '{mod_name}' удален.", 5000)
            except Exception as e:
                logging.error(f"Ошибка при удалении мода '{mod_name}': {e}")
                msg_box = CustomMessageBox(QMessageBox.Icon.Critical, "Ошибка удаления", f"Не удалось удалить мод: {e}", self)
                msg_box.addButton("ОК", QMessageBox.ButtonRole.AcceptRole)
                msg_box.exec()

    def load_config(self):
        """Загружает конфигурацию из файла."""
        # Инициализация путей для конфига и кэша
        self.app_name = "MinecraftModUpdater"
        self.config_dir = appdirs.user_data_dir(self.app_name, appauthor=False)
        self.config_file = os.path.join(self.config_dir, "config.json")
        self.cache_dir = os.path.join(self.config_dir, "cache")
        self.cache_file = os.path.join(self.cache_dir, "mod_cache.db")
        self.log_dir = os.path.join(self.config_dir, "logs") # Определяем путь к папке логов

        try:
            os.makedirs(self.config_dir, exist_ok=True) # Убедимся, что папка конфига существует
            with open(self.config_file, "r") as f:
                config = json.load(f)
                self.config = config # Сохраняем весь конфиг
                logging.info("Конфигурация успешно загружена.")

                # Миграция старого конфига или загрузка нового
                if "profiles" in config:
                    self.profiles = config["profiles"]
                    self.current_profile_name = config.get("current_profile", list(self.profiles.keys())[0] if self.profiles else "Default")
                else:
                    self.profiles = {
                        "Default": {
                            "mods_path": config.get("mods_path", ""),
                            "game_version": config.get("game_version", "1.21.11"),
                            "loader": config.get("loader", "fabric"),
                            "manual_mode": config.get("manual_mode", False)
                        }
                    }
                    self.current_profile_name = "Default"

                # Загружаем тему, но НЕ применяем. Применение будет в showEvent.
                self.config_theme = config.get("theme", "system")
        except FileNotFoundError:
            self.config = {} # Инициализируем пустой конфиг, если файл не найден
            self.profiles = {"Default": {"mods_path": "", "game_version": "1.21.11", "loader": "fabric", "manual_mode": False}}
            self.current_profile_name = "Default"
            logging.info("Файл конфигурации не найден, используются значения по умолчанию.")
        except json.JSONDecodeError:
            self.profiles = {"Default": {"mods_path": "", "game_version": "1.21.11", "loader": "fabric", "manual_mode": False}}
            self.current_profile_name = "Default"
            logging.warning("Ошибка при чтении файла конфигурации, файл будет перезаписан.")
        except Exception as e:
            logging.error(f"Непредвиденная ошибка при загрузке конфигурации: {e}")

        self._populate_profiles_combo()

    def save_config(self):
        """Сохраняет конфигурацию в файл."""
        self._save_ui_to_current_profile()
        config = {
            "theme": self.config_theme,
            "current_profile": self.current_profile_name,
            "profiles": self.profiles
        }

        # Создаем папку, если она не существует
        os.makedirs(self.config_dir, exist_ok=True)
        try:
            with open(self.config_file, "w") as f:
                json.dump(config, f)
            logging.info("Конфигурация успешно сохранена.")
        except Exception as e:
            logging.error(f"Ошибка при сохранении конфигурации: {e}")

    # --- Методы для работы с профилями ---

    def _populate_profiles_combo(self) -> None:
        """Заполняет выпадающий список профилей."""
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(self.profiles.keys())
        self.profile_combo.setCurrentText(self.current_profile_name)
        self.profile_combo.blockSignals(False)
        self._apply_profile_to_ui(self.current_profile_name)

    def _apply_profile_to_ui(self, profile_name: str) -> None:
        """Применяет настройки выбранного профиля к интерфейсу."""
        self._is_switching_profile = True
        prof = self.profiles.get(profile_name, {})
        # Сначала устанавливаем ручной режим
        self.manual_mode_checkbox.setChecked(prof.get("manual_mode", False))
        # Затем устанавливаем версии (если ручной режим включен, они не будут перезаписаны автоопределением)
        self.game_version_combo.setCurrentText(prof.get("game_version", "1.21.11"))
        self.loader_combo.setCurrentText(prof.get("loader", "fabric"))
        # Затем устанавливаем путь (это вызовет сканирование директории и автоопределение, если ручной режим выключен)
        new_path = prof.get("mods_path", "")
        old_path = self.path_input.text()
        self.path_input.setText(new_path)
        if new_path == old_path: self.check_mods_folder(new_path) # Принудительно вызываем, если текст (путь) не изменился
        logging.info(f"Загружен профиль: {profile_name}")
        self._is_switching_profile = False

    def _sync_table_to_mods_data(self):
        """Синхронизирует состояние UI (чекбоксы) с моделью данных."""
        for row in range(self.table.rowCount()):
            mod_data = self.row_to_data_map.get(row)
            if mod_data:
                cell_widget = self.table.cellWidget(row, TableColumn.CHECKBOX)
                if cell_widget and (checkbox := cell_widget.findChild(QCheckBox)):
                    mod_data['_is_checked'] = checkbox.isChecked()

    def _save_ui_to_current_profile(self) -> None:
        """Сохраняет текущие настройки интерфейса в активный профиль."""
        if not getattr(self, 'current_profile_name', None) or self.current_profile_name not in self.profiles:
            return
        self._sync_table_to_mods_data()
        self.profiles[self.current_profile_name] = {
            "mods_path": self.path_input.text(),
            "game_version": self.game_version_combo.currentText(),
            "loader": self.loader_combo.currentText(),
            "manual_mode": self.manual_mode_checkbox.isChecked()
        }

    def on_profile_changed(self, index: int) -> None:
        """Обработчик смены профиля в выпадающем списке."""
        if index < 0: return
        new_profile = self.profile_combo.itemText(index)
        if new_profile == self.current_profile_name: return
        
        self._save_ui_to_current_profile()
        self.current_profile_name = new_profile
        self._apply_profile_to_ui(self.current_profile_name)

    def add_profile(self) -> None:
        """Открывает диалог для добавления нового профиля."""
        name, ok = QInputDialog.getText(self, "Новый профиль", "Введите имя профиля:")
        if ok and name.strip():
            name = name.strip()
            if name in self.profiles:
                msg_box = CustomMessageBox(QMessageBox.Icon.Warning, "Ошибка", "Профиль с таким именем уже существует!", self)
                msg_box.addButton("ОК", QMessageBox.ButtonRole.AcceptRole)
                msg_box.exec()
                return
            self._save_ui_to_current_profile()
            self.profiles[name] = {"mods_path": "", "game_version": "1.21.11", "loader": "fabric", "manual_mode": False}
            self.current_profile_name = name
            self._populate_profiles_combo()

    def rename_profile(self) -> None:
        """Открывает диалог для переименования текущего профиля."""
        old_name = self.current_profile_name
        name, ok = QInputDialog.getText(self, "Переименовать профиль", "Введите новое имя:", text=old_name)
        if ok and name.strip() and name.strip() != old_name:
            name = name.strip()
            if name in self.profiles:
                msg_box = CustomMessageBox(QMessageBox.Icon.Warning, "Ошибка", "Профиль с таким именем уже существует!", self)
                msg_box.addButton("ОК", QMessageBox.ButtonRole.AcceptRole)
                msg_box.exec()
                return
            self._save_ui_to_current_profile()
            self.profiles[name] = self.profiles.pop(old_name)
            self.current_profile_name = name
            self._populate_profiles_combo()

    def delete_profile(self) -> None:
        """Удаляет текущий профиль (если он не единственный)."""
        if len(self.profiles) <= 1:
            msg_box = CustomMessageBox(QMessageBox.Icon.Warning, "Ошибка", "Нельзя удалить единственный профиль!", self)
            msg_box.addButton("ОК", QMessageBox.ButtonRole.AcceptRole)
            msg_box.exec()
            return
        
        msg_box = CustomMessageBox(QMessageBox.Icon.Question, "Удаление", f"Удалить профиль '{self.current_profile_name}'?", self)
        yes_btn = msg_box.addButton("Да", QMessageBox.ButtonRole.YesRole)
        no_btn = msg_box.addButton("Нет", QMessageBox.ButtonRole.NoRole)
        msg_box.setDefaultButton(no_btn)
        msg_box.exec()

        if msg_box.clickedButton() == yes_btn:
            del self.profiles[self.current_profile_name]
            self.current_profile_name = list(self.profiles.keys())[0]
            self._populate_profiles_combo()

    
    def update_scan_status(self, message: str):
        """Обновляет панель статуса сканирования/импорта."""
        self.progress_label.setText(message)
        
        # Попробуем извлечь прогресс из сообщения типа "Проверка: 5 / 100"
        match = re.search(r'(\d+)\s*/\s*(\d+)', message)
        if match:
            current, total = int(match.group(1)), int(match.group(2))
            if self.progress_bar.maximum() != total:
                self.progress_bar.setRange(0, total)
            
            self.progress_bar.setValue(current)
        else:
            # Если формат не совпадает, делаем прогресс-бар неопределенным
            if self.progress_bar.maximum() != 0:
                self.progress_bar.setRange(0, 0)



if __name__ == "__main__":
    # Определяем путь для логов до инициализации QApplication и ModUpdaterApp
    app_name_for_logging = "MinecraftModUpdater"
    config_dir_for_logging = appdirs.user_data_dir(app_name_for_logging, appauthor=False)
    log_dir_for_logging = os.path.join(config_dir_for_logging, "logs")
    os.makedirs(log_dir_for_logging, exist_ok=True) # Создаем папку для логов, если ее нет
    
    # Генерируем уникальное имя файла лога с отметкой времени
    timestamp_for_logging = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file_path_for_logging = os.path.join(log_dir_for_logging, f"mod_updater_{timestamp_for_logging}.log")

    # Настройка логирования
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(levelname)s - %(message)s', # Убран name
        filename=log_file_path_for_logging, # Используем динамически сгенерированный путь
        filemode='a', # Дописывать в этот уникальный файл
        encoding='utf-8' # Явно указываем кодировку для корректного отображения кириллицы
    )
    logging.info("Запуск приложения Minecraft Mod Updater.")
    app = QApplication(sys.argv)
    
    window = ModUpdaterApp()
    window.show()
    sys.exit(app.exec())
