"""Главное окно приложения: боковая панель со сборками, список модов, панель деталей и страницы."""
import logging
import os
import shutil
import webbrowser
from typing import Callable, Dict, Iterator, List, Optional

from PyQt6.QtCore import (
    QByteArray, QEasingCurve, QEvent, QItemSelectionModel, QObject, Qt, QTimer, QUrl, QVariantAnimation, pyqtSignal,
)
from PyQt6.QtGui import QDesktopServices, QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMenu,
    QMessageBox, QProgressBar, QStackedWidget, QVBoxLayout, QWidget,
)

import config as app_config
import icons
from content import CONTENT_TYPES, DISABLED_SUFFIX, MODS, ContentType
from enums import ModStatus, UpdateChannel
from exporters import (
    build_mrpack_index, build_share_data, decode_share_string, encode_share_string, parse_share_data,
    save_server_json, write_mrpack,
)
from game_detection import detect_game_versions, extract_loader_version
from launchers import discover_instances
from mod_updater_logic import format_date, get_latest_loader_version, select_primary_file
from scanner import (
    apply_install_info, collect_changelogs, entries_from_mrpack, mrpack_game_info, read_mrpack, resolve_project_install,
    scan_local,
)
from taskbar import TaskbarProgress
from theme import apply_window_frame_theme, build_stylesheet, detect_system_theme, make_tokens
from ui.catalog import CatalogPage
from ui.details_panel import DetailsPanel
from ui.dialogs import MigrationDialog, UpdateReviewDialog, ask_text
from ui.palette import Command, CommandPalette
from ui.mod_list import ModDelegate, ModFilterProxy, ModListModel, ModListView, entry_at, meta_at
from main_window_tools import ToolsMixin
from ui.pages import HistoryPage, LogPage, SettingsPage
from ui.stats_page import StatsPage
from ui.tools_dialogs import BisectBanner
from ui.widgets import (
    ChipButton, DropOverlay, ElidedLabel, EmptyState, SearchBox, SelectionBar, Sidebar, StatCard, ToastManager,
    button, label, make_action, profile_subtitle, refresh_button_icons, style_segments,
)
from ui_components import ImportDialog, ProfileDialog, ShareDialog, SyncDialog, ask_choice, confirm, show_message
from updater import (
    UpdateTransaction, add_files, backup_root, create_snapshot, delete_snapshot, find_locked_files, is_game_running,
    list_snapshots, list_transactions, move_all_to_backup, prune_snapshots, remove_mods, restore_snapshot,
    rollback_transaction, set_disabled,
)
from view_model import (
    CAT_ALL, CAT_DISABLED, CAT_INSTALL, CAT_NOT_FOUND, CAT_PROBLEMS, CAT_UPDATES, VIEW_FLAT, VIEW_MODES, VIEW_TREE,
    build_rows, category_counts, collect_tags, entry_key, format_size, is_updatable, iter_all_entries, pin_key,
    status_of,
)

from workers import (
    IconLoader, ImportWorker, LoaderVersionWorker, LocalScanWorker, ScanWorker, TaskWorker, UpdateWorker, fetch_json,
    fetch_versions,
)

LOADER_TITLES = {"fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge", "quilt": "Quilt"}
# Что ставит кнопка «Обновить все»: обновления и недостающие обязательные зависимости
BULK_STATUSES = (ModStatus.UPDATE_AVAILABLE, ModStatus.READY_TO_INSTALL, ModStatus.REQUIRED)
PAGE_MODS, PAGE_CATALOG, PAGE_HISTORY, PAGE_STATS, PAGE_LOG, PAGE_SETTINGS = range(6)
CONTENT_ICONS = {"mods": "package", "shaders": "sparkles", "resourcepacks": "image"}
ANIMATION_MS = 170


class LogEmitter(QObject):
    log_signal = pyqtSignal(str)


class QtLogHandler(logging.Handler):
    """Перенаправляет логи в журнал интерфейса через сигнал (потокобезопасно)."""

    def __init__(self, emitter: LogEmitter):
        super().__init__(level=logging.INFO)
        self.emitter = emitter

    def emit(self, record):
        try:
            self.emitter.log_signal.emit(self.format(record))
        except RuntimeError:
            pass


class ModUpdaterApp(ToolsMixin, QMainWindow):
    PAGE_MODS_INDEX, PAGE_CATALOG_INDEX, PAGE_HISTORY_INDEX, PAGE_STATS_INDEX = PAGE_MODS, PAGE_CATALOG, PAGE_HISTORY, PAGE_STATS
    PAGE_SETTINGS_INDEX = PAGE_SETTINGS

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Minecraft Mod Updater")
        self.setMinimumSize(1040, 660)
        self.resize(1320, 820)
        self.setAcceptDrops(True)

        cfg = app_config.load_config()
        self.config_theme: str = cfg.get("theme", "system")
        self.profiles: Dict[str, dict] = cfg["profiles"]
        self.current_profile_name: str = cfg["current_profile"]
        self.settings: dict = cfg["settings"]
        self.hidden_instances: List[str] = cfg["hidden_instances"]
        self.window_cfg: dict = cfg["window"]
        self.mod_tags: Dict[str, List[str]] = cfg["mod_tags"]
        self.mod_notes: Dict[str, str] = cfg["mod_notes"]
        self.favorites: List[str] = cfg["favorites"]
        self.last_seen: Dict[str, Dict[str, str]] = cfg["last_seen"]
        self._seen_baseline = {k: dict(v) for k, v in self.last_seen.items()}  # Что было при прошлом запуске
        self._undo: List[tuple] = []
        self._undoing = False
        self._minimal = False
        self.tokens = make_tokens(self._resolve_theme(), self.settings.get("accent"))

        # Состояние текущей сборки
        self.content: ContentType = MODS
        self.mods_path = ""  # Папка текущего типа контента (mods, shaderpacks или resourcepacks)
        self.game_version = ""
        self.loader = ""
        self.current_loader_version: Optional[str] = None
        self.latest_loader_version: Optional[str] = None
        self.mods_data: List[dict] = []
        self._count_items: List[dict] = []  # Записи для карточек-сводок (без повторов дерева)
        self.is_import_mode = False
        self.import_title = ""
        self.sessions: Dict[tuple, List[dict]] = {}  # (сборка, тип контента) -> последний список
        self._loaded_key: Optional[tuple] = None
        self._update_folder = ""
        self._catalog_installing: List[str] = []
        self._catalog_content: ContentType = MODS
        self._animations: Dict[object, QVariantAnimation] = {}
        self.taskbar: Optional[TaskbarProgress] = None
        self._busy: Optional[str] = None  # 'local' | 'scan' | 'update' | 'import' | 'task'
        self._silent_scan = False
        self._update_done = 0

        self.scan_worker: Optional[ScanWorker] = None
        self.local_scan_worker: Optional[LocalScanWorker] = None
        self.import_worker: Optional[ImportWorker] = None
        self.update_worker: Optional[UpdateWorker] = None
        self.loader_version_worker: Optional[LoaderVersionWorker] = None
        self._task_workers: List[TaskWorker] = []

        self.icon_loader = IconLoader(app_config.ICON_CACHE_DIR, self)
        self.icon_loader.icon_ready.connect(self._on_icon_ready)
        self._icon_pixmaps: Dict[str, QPixmap] = {}
        # Скриншоты галереи: миниатюры и полный размер в отдельных папках кэша
        cache_root = os.path.dirname(app_config.ICON_CACHE_DIR)
        self.thumb_loader = IconLoader(os.path.join(cache_root, "gallery_thumbs"), self, size=336)
        self.full_loader = IconLoader(os.path.join(cache_root, "gallery_full"), self, size=1600)

        self._build_ui()
        self.palette = CommandPalette(self.centralWidget(), self.tokens, self._palette_commands)
        self._setup_logging()
        self._setup_shortcuts()
        self._setup_query_completer()
        self.apply_theme()
        self._apply_view_settings()
        self._apply_minimal()
        self._restore_geometry()

        if self.settings.get("discover_launchers", True):
            self._merge_discovered(discover_instances())
        self._refresh_sidebar()
        QTimer.singleShot(0, lambda: self._load_profile(self.current_profile_name))
        QTimer.singleShot(4000, self._offer_install)

    # ================================================================== Построение интерфейса

    def _build_ui(self):
        t = self.tokens
        root = QWidget()
        root.setObjectName("ContentRoot")
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.sidebar = Sidebar(t, app_config.APP_VERSION)
        self.sidebar.profile_selected.connect(self._on_profile_selected)
        self.sidebar.profile_context_menu.connect(self._show_profile_menu)
        self.sidebar.add_profile_clicked.connect(self._show_add_profile_menu)
        self.sidebar.page_changed.connect(self._switch_page)
        self.sidebar.profiles_reordered.connect(self._on_profiles_reordered)
        layout.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.stack.addWidget(self._build_mods_page())
        self.catalog_page = CatalogPage(t, self._run_task, self._icon_for_entry, self.thumb_loader, self.full_loader)
        self.catalog_page.install_requested.connect(self._install_from_catalog)
        self.catalog_page.install_many_requested.connect(self._install_many_from_catalog)
        self.catalog_page.install_version_requested.connect(self._install_catalog_version)
        self.catalog_page.show_installed_requested.connect(self._show_installed_from_catalog)
        self.catalog_page.open_requested.connect(self._open_catalog_hit)
        self.catalog_page.installed_version = self._installed_version
        self.stack.addWidget(self.catalog_page)
        self.history_page = HistoryPage(t)
        self.history_page.rollback_requested.connect(self._rollback_from_history)
        self.history_page.snapshot_create_requested.connect(lambda: self._create_snapshot("manual"))
        self.history_page.snapshot_restore_requested.connect(self._restore_snapshot)
        self.history_page.snapshot_delete_requested.connect(self._delete_snapshot)
        self.history_page.open_backups_requested.connect(
            lambda: self.mods_path and self._open_path(backup_root(self.mods_path)))
        self.stack.addWidget(self.history_page)
        self.stats_page = StatsPage(t)
        self.stats_page.filter_requested.connect(self._stats_filter)
        self.stats_page.entry_requested.connect(self._show_entry_in_list)
        self.stats_page.replacement_requested.connect(self._find_replacement)
        self.stack.addWidget(self.stats_page)
        self.log_page = LogPage(t)
        self.log_page.open_logs_requested.connect(lambda: self._open_path(app_config.LOG_DIR))
        self.stack.addWidget(self.log_page)
        self.settings_page = SettingsPage(t)
        self.settings_page.load(self.config_theme, self.settings)
        self.settings_page.theme_changed.connect(self._on_theme_selected)
        self.settings_page.settings_changed.connect(self._on_settings_changed)
        self.settings_page.discover_requested.connect(self._discover_now)
        self.settings_page.clear_cache_requested.connect(self._clear_cache)
        self.settings_page.open_data_requested.connect(lambda: self._open_path(app_config.CONFIG_DIR))
        self.settings_page.export_requested.connect(self._export_settings)
        self.settings_page.import_requested.connect(self._import_settings)
        self.settings_page.prune_backups_requested.connect(lambda: self._prune_backups(manual=True))
        self.settings_page.install_requested.connect(self._install_app)
        self.settings_page.uninstall_requested.connect(self._uninstall_app)
        self.stack.addWidget(self.settings_page)

        self.toasts = ToastManager(root, t)
        self.drop_overlay = DropOverlay(root, t)

    def _build_mods_page(self) -> QWidget:
        t = self.tokens
        page = QWidget()
        page.setObjectName("Page")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 22, 28, 18)
        layout.setSpacing(14)

        # --- Шапка сборки ---
        header = QHBoxLayout()
        header.setSpacing(8)
        self.title_label = label("", "PageTitle")
        header.addWidget(self.title_label)
        self.edit_btn = button("", "IconButton", "pencil", t, "text_muted", "Настройки сборки")
        self.edit_btn.clicked.connect(self._edit_current_profile)
        header.addWidget(self.edit_btn)
        header.addStretch()
        self.check_btn = button("Проверить", None, "refresh", t, "text", "Проверить обновления (F5)")
        self.check_btn.clicked.connect(self._on_check_clicked)
        self.update_all_btn = button("Обновить все", "PrimaryButton", "download", t, "accent_text")
        self.update_all_btn.clicked.connect(self.update_all)
        self.more_btn = button("", "IconButton", "more", t, "text", "Импорт, экспорт и другое")
        self.more_btn.setFixedSize(38, 36)
        self.more_btn.clicked.connect(self._show_more_menu)
        for widget in (self.check_btn, self.update_all_btn, self.more_btn):
            widget.setMinimumHeight(36)
            header.addWidget(widget)
        layout.addLayout(header)

        tabs = QHBoxLayout()
        tabs.setSpacing(6)
        self.content_group = QButtonGroup(self)
        for i, content in enumerate(CONTENT_TYPES):
            tab = button(f" {content.title}", "Segment", CONTENT_ICONS[content.key], t, "text_muted")
            tab.setCheckable(True)
            self.content_group.addButton(tab, i)
            tabs.addWidget(tab)
        self.content_group.buttons()[0].setChecked(True)
        self.content_group.idClicked.connect(lambda i: self._set_content(CONTENT_TYPES[i]))
        tabs.addStretch()
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumHeight(32)
        self.preset_combo.setToolTip("Наборы модов: переключение включает и выключает нужные моды")
        self.preset_combo.activated.connect(lambda i: self.preset_combo.itemData(i) and self._apply_preset(self.preset_combo.itemData(i)))
        self.preset_btn = button(" Наборы", None, "layers", t, "text_muted", "Сохранить или применить набор модов")
        self.preset_btn.clicked.connect(self._show_preset_menu)
        tabs.addWidget(self.preset_combo)
        tabs.addWidget(self.preset_btn)
        self.tabs_widget = QWidget()
        self.tabs_widget.setLayout(tabs)
        tabs.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs_widget)

        chips = QHBoxLayout()
        chips.setSpacing(8)
        self.game_chip = ChipButton()
        self.game_chip.clicked.connect(self._edit_current_profile)
        self.loader_chip = ChipButton()
        self.loader_chip.clicked.connect(self._on_loader_chip_clicked)
        self.channel_chip = ChipButton()
        self.channel_chip.clicked.connect(self._show_channel_menu)
        for chip in (self.game_chip, self.loader_chip, self.channel_chip):
            chips.addWidget(chip)
        chips.addSpacing(6)
        self.path_icon = QLabel()
        chips.addWidget(self.path_icon)
        self.path_label = ElidedLabel()
        self.path_label.setObjectName("PathLabel")
        self.path_label.clicked.connect(lambda: self._open_path(self.mods_path or self._profile().get("mods_path", "")))
        chips.addWidget(self.path_label, 1)
        self.size_label = label("", "Faint")
        chips.addWidget(self.size_label)
        self.chips_widget = QWidget()
        self.chips_widget.setLayout(chips)
        chips.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.chips_widget)

        # --- Прогресс фоновой операции ---
        progress_row = QVBoxLayout()
        progress_row.setSpacing(4)
        self.slim_progress = QProgressBar()
        self.slim_progress.setObjectName("SlimProgress")
        self.slim_progress.setTextVisible(False)
        self.slim_progress.setRange(0, 0)
        self.status_label = label("", "Faint")
        progress_row.addWidget(self.slim_progress)
        progress_row.addWidget(self.status_label)
        self.progress_box = QWidget()
        self.progress_box.setLayout(progress_row)
        progress_row.setContentsMargins(0, 0, 0, 0)
        self.progress_box.hide()
        layout.addWidget(self.progress_box)

        # --- Баннер режима импорта ---
        self.banner = QFrame()
        self.banner.setObjectName("Banner")
        banner_layout = QHBoxLayout(self.banner)
        banner_layout.setContentsMargins(14, 10, 10, 10)
        self.banner_icon = QLabel()
        self.banner_text = label("", None, wrap=True)
        self.banner_cancel = button("Отменить импорт", "GhostButton", "x", t, "text_muted")
        self.banner_cancel.clicked.connect(self._exit_import_mode)
        banner_layout.addWidget(self.banner_icon)
        banner_layout.addWidget(self.banner_text, 1)
        banner_layout.addWidget(self.banner_cancel)
        self.banner.hide()
        layout.addWidget(self.banner)

        # --- Поиск проблемного мода (шаги) ---
        self.bisect_banner = BisectBanner(t)
        self.bisect_banner.answered.connect(self._on_bisect_answer)
        self.bisect_banner.cancel_requested.connect(self._cancel_bisect)
        self.bisect_banner.finish_requested.connect(self._finish_bisect)
        layout.addWidget(self.bisect_banner)

        # --- Карточки-сводки (фильтры) ---
        cards = QHBoxLayout()
        cards.setSpacing(10)
        self.card_group = QButtonGroup(self)
        self.card_group.setExclusive(True)
        self.cards: Dict[str, StatCard] = {}
        for category, title, color in ((CAT_ALL, "Все моды", "text"), (CAT_UPDATES, "Обновления", "accent"),
                                       (CAT_INSTALL, "К установке", "info"), (CAT_PROBLEMS, "Проблемы", "warning"),
                                       (CAT_NOT_FOUND, "Нет на Modrinth", "text_muted"), (CAT_DISABLED, "Выключены", "text_muted")):
            card = StatCard(title, category, color, t)
            card.clicked.connect(lambda _=False, c=category: self._set_category(c))
            self.card_group.addButton(card)
            self.cards[category] = card
            cards.addWidget(card)
        self.cards[CAT_ALL].setChecked(True)
        self.cards_widget = QWidget()
        self.cards_widget.setLayout(cards)
        cards.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.cards_widget)

        # --- Поиск и сортировка ---
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.search = SearchBox(t, "Поиск: название, файл, заметка или условие — tag:, side:client, is:fav, size>5mb")
        self.search.setMinimumHeight(36)
        self._search_timer = QTimer(self, singleShot=True, interval=150)
        self._search_timer.timeout.connect(lambda: self._apply_filter(query=self.search.text()))
        self.search.textChanged.connect(lambda: self._search_timer.start())
        toolbar.addWidget(self.search, 1)
        self.select_all = QCheckBox("Выбрать все")
        self.select_all.setTristate(True)
        self.select_all.clicked.connect(self._on_select_all_clicked)
        toolbar.addWidget(self.select_all)
        self.sort_combo = QComboBox()
        self.tag_combo = QComboBox()
        self.tag_combo.setMinimumHeight(36)
        self.tag_combo.setToolTip("Фильтр по метке: категории Modrinth, сторона (клиент/сервер) и ваши метки")
        self.tag_combo.currentIndexChanged.connect(self._on_tag_selected)
        toolbar.addWidget(self.tag_combo)
        self.sort_combo.addItem("Сначала важные", ModFilterProxy.SORT_STATUS)
        self.sort_combo.addItem("По названию", ModFilterProxy.SORT_NAME)
        self.sort_combo.addItem("Недавно обновлённые", ModFilterProxy.SORT_UPDATED)
        self.sort_combo.addItem("По размеру", ModFilterProxy.SORT_SIZE)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        self.sort_combo.setMinimumHeight(36)
        toolbar.addWidget(self.sort_combo)
        self.toolbar_widget = QWidget()
        self.toolbar_widget.setLayout(toolbar)
        toolbar.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.toolbar_widget)

        # --- Список и панель деталей ---
        body = QHBoxLayout()
        body.setSpacing(14)
        self.list_holder = QWidget()
        holder_layout = QVBoxLayout(self.list_holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        self.list_stack = QStackedWidget()
        holder_layout.addWidget(self.list_stack)

        self.model = ModListModel(self)
        self.proxy = ModFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.view = ModListView()
        self.view.setModel(self.proxy)
        self.delegate = ModDelegate(self.view, t, self._icon_for_entry)
        self.view.setItemDelegate(self.delegate)
        self.delegate.check_toggled.connect(lambda _: self._refresh_selection_ui())
        self.delegate.update_clicked.connect(lambda e: self.start_update([e]))
        self.delegate.menu_requested.connect(self._show_entry_menu)
        self.delegate.expand_toggled.connect(lambda e: self._set_expanded(e, not self.proxy.is_expanded(e)))
        self.delegate.is_expanded = self.proxy.is_expanded
        self.view.expand_requested.connect(self._expand_current)
        self.view.open_requested.connect(self._open_current_details)
        self.view.doubleClicked.connect(lambda _: self._open_current_details())
        self.view.context_menu_requested.connect(self._show_entry_menu)
        self.view.toggle_requested.connect(self._toggle_current)
        self.view.delete_requested.connect(self._delete_checked_or_current)
        self.view.selectionModel().currentChanged.connect(self._on_current_changed)
        self.list_stack.addWidget(self.view)
        self.empty_state = EmptyState()
        self.list_stack.addWidget(self.empty_state)

        self.selection_bar = SelectionBar(self.list_holder, t)
        self.selection_bar.update_clicked.connect(self._update_checked)
        self.selection_bar.delete_clicked.connect(self._delete_checked)
        self.selection_bar.disable_clicked.connect(lambda: self._toggle_disabled(
            [e for e in self._checked_entries() if e.get("filepath")]))
        self.selection_bar.clear_clicked.connect(lambda: self._set_all_checked(False))
        self.selection_bar.more_clicked.connect(self._show_bulk_menu)
        self.list_holder.installEventFilter(self)
        body.addWidget(self.list_holder, 1)

        self.details = DetailsPanel(t)
        self.details.hide()
        self.details.closed.connect(self._close_details)
        self.details.install_requested.connect(lambda e: self.start_update([e]))
        self.details.delete_requested.connect(lambda e: self._delete_entries([e]))
        self.details.open_page_requested.connect(self._open_mod_page)
        self.details.open_file_requested.connect(self._show_in_folder)
        self.details.versions_requested.connect(self._load_versions)
        self.details.version_install_requested.connect(self._install_specific_version)
        self.details.select_entry_requested.connect(self._select_entry)
        self.details.pin_toggled.connect(self._toggle_pin)
        self.details.disable_toggled.connect(lambda e: self._toggle_disabled([e]))
        self.details.tags_menu_requested.connect(self._show_tags_menu)
        self.details.favorite_toggled.connect(lambda e: self._toggle_favorite([e]))
        self.details.note_changed.connect(self._set_note)
        self.details.replacement_requested.connect(self._find_replacement)
        self.details.attach_gallery(self.thumb_loader, self.full_loader)
        self.details.custom_tags = self.mod_tags
        self.proxy.custom_tags = self.mod_tags
        body.addWidget(self.details)
        layout.addLayout(body, 1)
        return page

    def _setup_logging(self):
        self.log_emitter = LogEmitter()
        self._gui_log_handler = QtLogHandler(self.log_emitter)
        self._gui_log_handler.setFormatter(logging.Formatter('%(asctime)s  %(levelname)-7s  %(message)s', "%H:%M:%S"))
        logging.getLogger().addHandler(self._gui_log_handler)
        self.log_emitter.log_signal.connect(self.log_page.console.appendPlainText)

    def _setup_shortcuts(self):
        QShortcut(QKeySequence("Escape"), self, activated=self._on_escape)
        QShortcut(QKeySequence("Ctrl+P"), self, activated=self.palette.open)  # Привычная альтернатива Ctrl+K
        self._setup_action_shortcuts()  # Переназначаемые (настройки → «Горячие клавиши»)

    # ================================================================== Тема и окно

    def _resolve_theme(self) -> str:
        return detect_system_theme() if self.config_theme == "system" else self.config_theme

    def apply_theme(self):
        t = self.tokens = make_tokens(self._resolve_theme(), self.settings.get("accent"))
        QApplication.instance().setStyleSheet(build_stylesheet(t, self._theme_assets(t)))
        for widget in (self.sidebar, self.history_page, self.log_page, self.settings_page, self.details, self.search,
                       self.drop_overlay, self.toasts, self.catalog_page, self.palette, self.stats_page):
            widget.set_tokens(t)
        refresh_button_icons(self.bisect_banner, t)
        style_segments(self.content_group, t)
        for card in self.cards.values():
            card.set_tokens(t)
        self.delegate.set_tokens(t)
        refresh_button_icons(self.stack.widget(PAGE_MODS), t)
        refresh_button_icons(self.selection_bar, t)
        self.path_icon.setPixmap(icons.pixmap("folder", t["text_faint"], 14))
        self.banner_icon.setPixmap(icons.pixmap("import", t["info"], 18))
        self.view.viewport().update()
        apply_window_frame_theme(self, t)
        self._refresh_empty_state()

    @staticmethod
    def _theme_assets(t: dict) -> str:
        """SVG-файлы для таблицы стилей (галочка чекбокса, стрелка списка). Возвращает путь к галочке."""
        directory = os.path.join(os.path.dirname(app_config.ICON_CACHE_DIR), "ui_assets")
        try:
            os.makedirs(directory, exist_ok=True)
            check = os.path.join(directory, f"check_{t['name']}_{t['accent_text'].lstrip('#')}.svg")
            chevron = os.path.join(directory, f"chevron_{t['name']}.svg")
            with open(check, "wb") as f:
                f.write(icons.svg_bytes("check", t["accent_text"]))
            with open(chevron, "wb") as f:
                f.write(icons.svg_bytes("chevron_down", t["text_muted"]))
            return check.replace("\\", "/")
        except OSError as e:
            logging.debug(f"Не удалось подготовить ресурсы темы: {e}")
            return ""

    def _on_theme_selected(self, theme: str):
        self.config_theme = theme
        self.apply_theme()

    def showEvent(self, event):
        super().showEvent(event)
        apply_window_frame_theme(self, self.tokens)
        if self.taskbar is None:
            self.taskbar = TaskbarProgress(int(self.winId()))

    def _restore_geometry(self):
        geometry = self.window_cfg.get("geometry")
        if geometry:
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode()))

    def eventFilter(self, obj, event):
        if obj is self.list_holder and event.type() == QEvent.Type.Resize:
            self._position_selection_bar()
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.drop_overlay.setGeometry(self.centralWidget().rect().adjusted(260, 12, -12, -12))
        from ui.details_panel import panel_width
        self.details.setFixedWidth(panel_width(self.stack.width() - 56))
        if self.palette.isVisible():
            self.palette.open()

    def closeEvent(self, event):
        workers = [w for w in (self.scan_worker, self.import_worker, self.local_scan_worker, self.update_worker,
                               self.loader_version_worker, *self._task_workers) if w and w.isRunning()]
        if self.update_worker and self.update_worker.isRunning():
            if not confirm(self, "Идёт обновление", "Моды ещё скачиваются. Прервать и закрыть программу?",
                           "Прервать и закрыть", "Продолжить"):
                event.ignore()
                return
        for worker in workers:
            if hasattr(worker, "stop"):
                worker.stop()
        for worker in workers:
            worker.wait(10000)
        for loader in (self.icon_loader, self.thumb_loader, self.full_loader):
            loader.shutdown()
        self.window_cfg["geometry"] = bytes(self.saveGeometry().toBase64()).decode()
        self.save_config()
        logging.getLogger().removeHandler(self._gui_log_handler)
        super().closeEvent(event)

    # ================================================================== Конфигурация и сборки

    def save_config(self):
        try:
            app_config.save_config({
                "theme": self.config_theme, "current_profile": self.current_profile_name, "profiles": self.profiles,
                "settings": self.settings, "hidden_instances": self.hidden_instances, "window": self.window_cfg,
                "mod_tags": self.mod_tags, "mod_notes": self.mod_notes, "favorites": self.favorites,
                "last_seen": self.last_seen,
            })
        except OSError as e:
            logging.error(f"Ошибка при сохранении конфигурации: {e}")

    def _profile(self) -> dict:
        return self.profiles.get(self.current_profile_name) or app_config.default_profile()

    def _merge_discovered(self, instances) -> int:
        """Добавляет найденные в лаунчерах сборки и обновляет версии уже известных."""
        def norm(path):
            return os.path.normcase(os.path.normpath(path or ""))

        by_dir = {norm(p.get("mods_path")): name for name, p in self.profiles.items() if p.get("mods_path")}
        hidden = {norm(p) for p in self.hidden_instances}
        added = 0
        for inst in instances:
            key = norm(inst.game_dir)
            if key in hidden:
                continue
            if key in by_dir:
                profile = self.profiles[by_dir[key]]
                if profile.get("source", "manual") not in ("manual",) and inst.game_version:
                    profile.update({k: v for k, v in inst.to_profile(profile.get("channel")).items()
                                    if k in ("game_version", "loader", "loader_version") and v})
                continue
            name = inst.name
            suffix = 2
            while name in self.profiles:
                name = f"{inst.name} ({suffix})"
                suffix += 1
            self.profiles[name] = inst.to_profile(app_config.DEFAULT_CHANNEL.value)
            by_dir[key] = name
            added += 1
        # Пустой профиль по умолчанию больше не нужен, если нашлись настоящие сборки
        default = self.profiles.get("Default")
        if added and default is not None and not default.get("mods_path") and len(self.profiles) > 1:
            del self.profiles["Default"]
            if self.current_profile_name == "Default":
                self.current_profile_name = next(iter(self.profiles))
        return added

    def _refresh_sidebar(self):
        items = []
        for name, profile in self.profiles.items():
            items.append({"name": name, "subtitle": profile_subtitle(profile),
                          "tooltip": profile.get("mods_path") or "Папка не указана", "group": profile.get("group") or "",
                          "color": profile.get("color") or "", "icon": profile.get("icon") or ""})
        self.sidebar.set_profiles(items, self.current_profile_name)

    def _on_profile_selected(self, name: str):
        if name == self.current_profile_name:
            return
        if self._busy:
            self.toasts.show("Дождитесь окончания текущей операции.", "warning")
            self._refresh_sidebar()
            return
        self._load_profile(name)

    def _skey(self, profile: Optional[str] = None, content: Optional[ContentType] = None) -> tuple:
        return profile or self.current_profile_name, (content or self.content).key

    def _drop_sessions(self, profile: str):
        for key in [k for k in self.sessions if k[0] == profile]:
            del self.sessions[key]

    def _remember_session(self):
        """Запоминает список текущей вкладки, чтобы при возврате не сканировать заново."""
        if self._loaded_key and not self.is_import_mode and self._busy is None:
            self.sessions[self._loaded_key] = self.mods_data

    def _set_content(self, content: ContentType):
        if content is self.content:
            return
        if self._busy:
            self.toasts.show("Дождитесь окончания текущей операции.", "warning")
            self.content_group.buttons()[CONTENT_TYPES.index(self.content)].setChecked(True)
            return
        if self.is_import_mode:
            self._exit_import_mode()
        self._remember_session()
        self.content = content
        self.content_group.buttons()[CONTENT_TYPES.index(content)].setChecked(True)
        style_segments(self.content_group, self.tokens)
        self._close_details()
        self._load_content()

    def _load_content(self):
        """Показывает текущий тип контента: из памяти или с локальным сканированием папки."""
        game_dir = self._profile().get("mods_path", "")
        self.mods_path = self.content.path(game_dir)
        self._loaded_key = self._skey()
        self._update_header()
        self.cards[CAT_ALL].title = f"Все {self.content.title.lower()}"
        self.cards[CAT_ALL].update()
        if not game_dir or not os.path.isdir(game_dir):
            self._set_data([])
            return
        cached = self.sessions.get(self._loaded_key)
        if cached is not None:
            self._set_data(cached)
        else:
            self._set_data([])
            self.start_local_scan()

    def _load_profile(self, name: str):
        if name not in self.profiles:
            name = next(iter(self.profiles))
        self._remember_session()
        self.current_profile_name = name
        self.is_import_mode = False
        self.banner.hide()
        self._close_details()
        self.sidebar.set_page(PAGE_MODS)
        self.stack.setCurrentIndex(PAGE_MODS)

        profile = self._profile()
        self._resolve_versions(profile)
        self._start_loader_check()
        self._restore_profile_view()
        self._load_content()
        self._restore_bisect_banner()

    def _resolve_versions(self, profile: dict):
        """Определяет версию игры и загрузчик: из настроек сборки или по папке versions."""
        self.game_version = profile.get("game_version", "")
        self.loader = profile.get("loader", "")
        self.current_loader_version = profile.get("loader_version") or None
        self.latest_loader_version = None
        game_dir = profile.get("mods_path", "")
        if not profile.get("manual_mode") and game_dir and os.path.isdir(game_dir):
            detected = detect_game_versions(game_dir)
            if detected.best_guess:
                self.game_version, self.loader = detected.best_guess
                self.current_loader_version = extract_loader_version(self.loader, detected.best_guess_id or "")
                if (profile.get("game_version"), profile.get("loader")) != detected.best_guess:
                    profile["game_version"], profile["loader"] = detected.best_guess
                    self._refresh_sidebar()

    def _channel(self) -> UpdateChannel:
        return UpdateChannel.from_value(self._profile().get("channel"), app_config.DEFAULT_CHANNEL)

    def _update_header(self):
        t = self.tokens
        profile = self._profile()
        self.title_label.setText(self.current_profile_name)
        self.game_chip.setText(f"Minecraft {self.game_version or '?'}")
        self.game_chip.setToolTip("Изменить версию игры и загрузчик")
        loader_title = LOADER_TITLES.get(self.loader, self.loader or "Загрузчик ?")
        version = f" {self.current_loader_version}" if self.current_loader_version else ""
        has_update = bool(self.latest_loader_version and self.current_loader_version
                          and self._is_newer(self.latest_loader_version, self.current_loader_version))
        self.loader_chip.setText(f"{loader_title}{version}" + (f"  →  {self.latest_loader_version}" if has_update else ""))
        self.loader_chip.set_variant(warning=has_update)
        self.loader_chip.setToolTip("Доступна новая версия загрузчика — обновите её в лаунчере" if has_update
                                    else "Версия загрузчика")
        self.channel_chip.setText(f"{self._channel().display_name}  ▾")
        self.channel_chip.setToolTip("Какие версии модов предлагать: только релизы, с бетами или все")
        self.path_label.setFullText(profile.get("mods_path") or "Папка не указана")
        self.path_icon.setPixmap(icons.pixmap("folder", t["text_faint"], 14))

    @staticmethod
    def _is_newer(a: str, b: str) -> bool:
        from packaging.version import InvalidVersion, parse
        try:
            return parse(a) > parse(b)
        except InvalidVersion:
            return a != b

    def _start_loader_check(self):
        if not (self.loader and self.game_version and self.current_loader_version):
            return
        worker = LoaderVersionWorker(self.loader, self.game_version, self.current_loader_version)
        worker.finished.connect(self._on_loader_checked)
        self.loader_version_worker = worker
        worker.start()

    def _on_loader_checked(self, message, color_key, latest):
        if self.sender() is not self.loader_version_worker:
            return
        self.latest_loader_version = latest or None
        self._update_header()

    def _on_loader_chip_clicked(self):
        if self.latest_loader_version and self.current_loader_version and \
                self._is_newer(self.latest_loader_version, self.current_loader_version):
            self.toasts.show(f"Доступен {LOADER_TITLES.get(self.loader, self.loader)} {self.latest_loader_version} "
                             f"(у вас {self.current_loader_version}). Загрузчик обновляется в лаунчере.", "info", timeout=8000)
        else:
            self._edit_current_profile()

    def _show_channel_menu(self):
        menu = QMenu(self)
        current = self._channel()
        for channel in UpdateChannel:
            action = menu.addAction(("✓  " if channel == current else "     ") + channel.display_name)
            action.triggered.connect(lambda _=False, c=channel: self._set_channel(c))
        menu.exec(self.channel_chip.mapToGlobal(self.channel_chip.rect().bottomLeft()))

    def _set_channel(self, channel: UpdateChannel):
        if channel == self._channel():
            return
        self._profile()["channel"] = channel.value
        self._update_header()
        if self.mods_data and not self.is_import_mode:
            self.start_scan()

    def _show_add_profile_menu(self):
        t = self.tokens
        menu = QMenu(self)
        menu.addAction(make_action(menu, t, "folder", "Указать папку игры…", self._add_profile))
        menu.addAction(make_action(menu, t, "box_search", "Найти сборки в лаунчерах", self._discover_now))
        menu.exec(self.sidebar.add_btn.mapToGlobal(self.sidebar.add_btn.rect().bottomLeft()))

    def _channel_choices(self):
        return [(c.value, c.display_name) for c in UpdateChannel]

    def _add_profile(self):
        dialog = ProfileDialog(self, "", app_config.default_profile(), self.profiles.keys(), self._channel_choices(),
                               self._profile_groups())
        if dialog.exec():
            name, profile = dialog.result_profile()
            self.profiles[name] = profile
            self._refresh_sidebar()
            self._load_profile(name)
            self._refresh_sidebar()

    def _edit_current_profile(self):
        self._edit_profile(self.current_profile_name)

    def _edit_profile(self, old_name: str):
        if self._busy or old_name not in self.profiles:
            return
        dialog = ProfileDialog(self, old_name, self.profiles[old_name], self.profiles.keys(), self._channel_choices(),
                               self._profile_groups())
        if not dialog.exec():
            return
        name, profile = dialog.result_profile()
        if profile.get("mods_path") != self.profiles[old_name].get("mods_path"):
            profile["source"] = "manual"
        self.profiles = {(name if k == old_name else k): (profile if k == old_name else v) for k, v in self.profiles.items()}
        self._drop_sessions(old_name)
        if self.current_profile_name == old_name:
            self.current_profile_name = name
            self._refresh_sidebar()
            self._load_profile(name)
        self._refresh_sidebar()

    def _show_profile_menu(self, name: str, pos):
        t = self.tokens
        menu = QMenu(self)
        menu.addAction(make_action(menu, t, "pencil", "Настройки сборки…", lambda: self._edit_profile(name)))
        menu.addAction(make_action(menu, t, "folder", "Открыть папку",
                                   lambda: self._open_path(self.profiles[name].get("mods_path", ""))))
        menu.addAction(make_action(menu, t, "layers", "Группа…", lambda: self._set_profile_group(name)))
        menu.addSeparator()
        menu.addAction(make_action(menu, t, "copy", "Клонировать сборку…", lambda: self._clone_profile(name)))
        others = [n for n in self.profiles if n != name]
        if others:
            compare = menu.addMenu(icons.icon("git_compare", t["text_muted"], 16), "Сравнить с…")
            for other in others:
                compare.addAction(other, lambda o=other: self._compare_profiles(name, o))
        menu.addSeparator()
        menu.addAction(make_action(menu, t, "x", "Убрать из списка", lambda: self._remove_profile(name), "danger"))
        menu.exec(pos)

    def _remove_profile(self, name: str):
        if len(self.profiles) <= 1:
            self.toasts.show("Нельзя убрать единственную сборку.", "warning")
            return
        if not confirm(self, "Убрать сборку", f"Убрать «{name}» из списка?\nФайлы игры и моды не будут удалены.",
                       "Убрать", "Отмена"):
            return
        profile = self.profiles.pop(name)
        if profile.get("source", "manual") != "manual" and profile.get("mods_path"):
            self.hidden_instances.append(profile["mods_path"])  # Не добавлять снова при автопоиске
        self._drop_sessions(name)
        if name == self.current_profile_name:
            self.current_profile_name = next(iter(self.profiles))
            self._load_profile(self.current_profile_name)
        self._refresh_sidebar()

    def _discover_now(self):
        self.hidden_instances = []
        added = self._merge_discovered(discover_instances())
        self._refresh_sidebar()
        self.toasts.show(f"Найдено новых сборок: {added}" if added else "Новых сборок не найдено.",
                         "success" if added else "info")

    def _on_settings_changed(self, settings: dict):
        view_keys = ("view_mode", "compact_rows", "expand_dependencies", "group_by_status", "columns", "abandoned_warning")
        view_changed = any(self.settings.get(k) != settings.get(k) for k in view_keys if k in settings)
        accent_changed = settings.get("accent") != self.settings.get("accent")
        scale_changed = settings.get("ui_scale") != self.settings.get("ui_scale")
        minimal_changed = settings.get("minimal_mode") != self.settings.get("minimal_mode")
        shortcuts_changed = settings.get("shortcuts") != self.settings.get("shortcuts")
        if self.settings.get("per_profile_view", True) and "view_mode" in settings:
            self._profile()["view_mode"] = settings["view_mode"]  # Вид запоминается для текущей сборки
        self.settings.update(settings)
        if accent_changed:
            self.apply_theme()
        if view_changed:
            self._rebuild_view()
        if minimal_changed:
            self._apply_minimal()
        if shortcuts_changed:
            self._setup_action_shortcuts()
        if scale_changed:
            self.toasts.show(f"Масштаб {settings.get('ui_scale')}% применится после перезапуска.", "info",
                             "Перезапустить", self._restart_app, timeout=10000)

    def _clear_cache(self):
        if self._busy:
            self.toasts.show("Дождитесь окончания текущей операции.", "warning")
            return
        for path in (app_config.CACHE_FILE,):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError as e:
                logging.error(f"Не удалось удалить {path}: {e}")
        shutil.rmtree(app_config.ICON_CACHE_DIR, ignore_errors=True)
        os.makedirs(app_config.ICON_CACHE_DIR, exist_ok=True)
        self._icon_pixmaps.clear()
        self.icon_loader._pending.clear()
        self.sessions.clear()
        self.toasts.show("Кэш очищен.", "success")

    # ================================================================== Страницы

    def _switch_page(self, page: int):
        self.sidebar.set_page(page)
        self.stack.setCurrentIndex(page)
        if page == PAGE_HISTORY:
            self._refresh_history()
        elif page == PAGE_CATALOG:
            self._refresh_catalog_context()
        elif page == PAGE_STATS:
            self._refresh_stats()
        elif page == PAGE_SETTINGS:
            self.settings_page.load(self.config_theme, {**self.settings, "view_mode": self._view_mode()})
            self.settings_page.set_backup_usage(self._backup_usage_text())
            self._refresh_install_ui()

    def _refresh_history(self):
        if not self.mods_path:
            self.history_page.set_transactions([], [])
            self.history_page.usage_label.setText("")
            return
        self.history_page.set_transactions(list_transactions(self.mods_path), list_snapshots(self.mods_path))
        self.history_page.usage_label.setText(self._backup_usage_text())

    # ================================================================== Данные списка

    def _all_entries(self) -> Iterator[dict]:
        seen = set()
        for entry in list(iter_all_entries(self.mods_data)) + self.model.items():
            if id(entry) not in seen:
                seen.add(id(entry))
                yield entry

    def _set_data(self, mods_data: List[dict], keep_selection: bool = True):
        """Обновляет список модов, сохраняя выделение и открытую панель деталей."""
        selected_key = entry_key(self.details.entry) if keep_selection and self.details.entry else None
        self.mods_data = mods_data
        self._apply_view_settings()
        pinned = self._profile().get("pinned") or {}
        for entry in iter_all_entries(mods_data):
            entry["_pinned"] = bool(entry.get("filepath")) and pin_key(entry) in pinned
        self._apply_personal_flags(mods_data)
        rows, self._count_items = build_rows(mods_data, self._view_mode())
        self.model.set_rows(rows)
        items = self.model.items()
        self.proxy.sort(0)
        self._refresh_counts()
        self._refresh_selection_ui()
        self._refresh_empty_state()
        self._refresh_actions()
        self._refresh_presets()
        if selected_key is not None:
            match = next((e for e in items if entry_key(e) == selected_key), None)
            if match:
                self._select_entry(match)
            else:
                self._close_details()

    def _refresh_counts(self):
        counts = category_counts(self._count_items)
        for category, card in self.cards.items():
            card.set_count(counts.get(category, 0))
        if self.is_import_mode:
            visible = {CAT_ALL, CAT_INSTALL, CAT_PROBLEMS}
        else:
            visible = {CAT_ALL, CAT_UPDATES, CAT_PROBLEMS, CAT_NOT_FOUND}
            if counts.get(CAT_INSTALL):
                visible.add(CAT_INSTALL)
            if counts.get(CAT_DISABLED):
                visible.add(CAT_DISABLED)
        installed = [e for e in self._count_items if e.get("filepath")]
        total = sum(e.get("size") or 0 for e in installed)
        self.size_label.setText(f"{len(installed)} файлов  ·  {format_size(total)}" if installed else "")
        self._refresh_tag_combo()
        for category, card in self.cards.items():
            card.setVisible(category in visible)
        if not self.cards[self.proxy.category].isVisible():
            self._set_category(CAT_ALL)

    def _set_category(self, category: str):
        self.cards[category].setChecked(True)
        self._apply_filter(category=category)

    def _apply_filter(self, category: Optional[str] = None, query: Optional[str] = None):
        self.proxy.set_filter(category, query)
        self._refresh_empty_state()
        self._refresh_selection_ui()

    def _refresh_empty_state(self):
        t = self.tokens
        profile = self._profile()
        game_dir = profile.get("mods_path", "")
        has_items = bool(self.model.items())
        self.cards_widget.setVisible(has_items and not self._minimal)
        self.toolbar_widget.setVisible(has_items)
        if self.proxy.rowCount() > 0:
            self.list_stack.setCurrentIndex(0)
            return
        if not game_dir or not os.path.isdir(game_dir):
            b1 = button("Указать папку игры", "PrimaryButton", "folder", t, "accent_text")
            b1.clicked.connect(self._edit_current_profile)
            b2 = button("Найти в лаунчерах", None, "box_search", t, "text")
            b2.clicked.connect(self._discover_now)
            self.empty_state.set_content(t, "folder", "Папка игры не найдена",
                                         "Укажите папку игры, в которой лежит папка mods, или найдите сборки "
                                         "в Prism Launcher, Modrinth App и CurseForge.", [b1, b2])
        elif self._busy in ("local", "import") and not self.model.items():
            self.empty_state.set_content(t, "refresh", "Загрузка…", "Читаем список модов.")
        elif not self.model.items():
            b1 = button("Импортировать сборку", None, "import", t, "text")
            b1.clicked.connect(self.import_mod_list)
            b2 = button("Открыть .mrpack", None, "package", t, "text")
            b2.clicked.connect(self.import_mrpack_dialog)
            self.empty_state.set_content(t, "drop", "Модов пока нет",
                                         "Перетащите сюда .jar-файлы или .mrpack, вставьте строку сборки "
                                         "или импортируйте список от друга.", [b1, b2])
        else:
            b1 = button("Сбросить фильтры", None, "x", t, "text")
            b1.clicked.connect(self._reset_filters)
            self.empty_state.set_content(t, "search", "Ничего не найдено",
                                         "Попробуйте изменить запрос или выбрать другой фильтр.", [b1])
        self.list_stack.setCurrentIndex(1)

    def _reset_filters(self):
        self.search.clear()
        self.tag_combo.setCurrentIndex(0)
        self._set_category(CAT_ALL)

    def _refresh_actions(self):
        busy = self._busy is not None
        bulk = self._bulk_candidates()
        verb = "Установить все" if self.is_import_mode else "Обновить все"
        self.update_all_btn.setText(f"{verb} ({len(bulk)})" if bulk else verb)
        self.update_all_btn.setEnabled(bool(bulk) and not busy)
        scanning = self._busy in ("scan", "local", "import")
        self.check_btn.setText("Отмена" if scanning else "Проверить")
        self.check_btn.setProperty("icon_name", "x" if scanning else "refresh")
        self.check_btn.setIcon(icons.icon("x" if scanning else "refresh", self.tokens["text"], 16))
        self.check_btn.setEnabled((scanning or not busy) and bool(self.mods_path) and not self.is_import_mode)
        self.more_btn.setEnabled(not busy)
        self.edit_btn.setEnabled(not busy)
        self.sidebar.add_btn.setEnabled(not busy)
        self.selection_bar.update_btn.setEnabled(not busy and self.selection_bar.update_btn.isEnabled())

    def _bulk_candidates(self) -> List[dict]:
        return [e for e in self.model.items() if is_updatable(e) and status_of(e) in BULK_STATUSES]

    def _refresh_tag_combo(self):
        current = self.tag_combo.currentData()
        tags = collect_tags(self._count_items, self.mod_tags)
        self.tag_combo.blockSignals(True)
        self.tag_combo.clear()
        self.tag_combo.addItem("Все метки", None)
        for key, title, count in tags:
            self.tag_combo.addItem(f"{title}  ({count})", key)
        index = self.tag_combo.findData(current)
        self.tag_combo.setCurrentIndex(max(0, index))
        self.tag_combo.blockSignals(False)
        self.tag_combo.setVisible(len(tags) > 0)
        if current and index < 0:
            self._on_tag_selected()

    def _on_tag_selected(self, *_):
        self.proxy.set_tag(self.tag_combo.currentData())
        self.proxy.sort(0)
        self._refresh_empty_state()
        self._refresh_selection_ui()

    def _on_sort_changed(self, *_):
        self.proxy.set_sort_mode(self.sort_combo.currentData())
        if self.settings.get("per_profile_view", True):
            self._profile()["sort"] = self.sort_combo.currentData()
        self.view.relayout()

    def _restore_profile_view(self):
        """Вид и сортировка, сохранённые для этой сборки."""
        if not self.settings.get("per_profile_view", True):
            return
        sort = self._profile().get("sort")
        index = self.sort_combo.findData(sort) if sort else 0
        if index >= 0 and index != self.sort_combo.currentIndex():
            self.sort_combo.blockSignals(True)
            self.sort_combo.setCurrentIndex(index)
            self.sort_combo.blockSignals(False)
            self.proxy.set_sort_mode(self.sort_combo.currentData())

    def _icon_for_entry(self, entry: dict) -> Optional[QPixmap]:
        pid = entry.get("project_id")
        if not pid:
            return None
        pm = self._icon_pixmaps.get(pid)
        if pm is None:
            path = self.icon_loader.request(pid, entry.get("icon_url"))
            if path:
                pm = QPixmap(path)
                self._icon_pixmaps[pid] = pm
        return pm

    def _on_icon_ready(self, project_id: str, path: str):
        self._icon_pixmaps[project_id] = QPixmap(path)
        self.model.refresh_where(lambda e: e.get("project_id") == project_id)
        if self.details.entry and self.details.entry.get("project_id") == project_id:
            self.details.set_entry(self.details.entry, self._icon_pixmaps[project_id])

    def _refresh_project(self, project_id: Optional[str]):
        if project_id:
            self.model.refresh_where(lambda e: e.get("project_id") == project_id)
        else:
            self.model.refresh_all()

    # ================================================================== Выделение и панель деталей

    def _visible_entries(self) -> List[dict]:
        return [entry_at(self.proxy.index(r, 0)) for r in range(self.proxy.rowCount())]

    def _checked_entries(self) -> List[dict]:
        return [e for e in self.model.items() if e.get("_is_checked")]

    def _refresh_selection_ui(self):
        checked = self._checked_entries()
        visible = self._visible_entries()
        visible_checked = sum(1 for e in visible if e.get("_is_checked"))
        self.select_all.blockSignals(True)
        state = (Qt.CheckState.Unchecked if visible_checked == 0 else
                 Qt.CheckState.Checked if visible_checked == len(visible) else Qt.CheckState.PartiallyChecked)
        self.select_all.setCheckState(state)
        self.select_all.setEnabled(bool(visible))
        self.select_all.blockSignals(False)
        if checked:
            files = [e for e in checked if e.get("filepath")]
            self.selection_bar.set_state(len(checked), sum(1 for e in checked if e.get("download_url")),
                                         len(files), self.is_import_mode,
                                         all_disabled=bool(files) and all(e.get("disabled") for e in files))
            self.selection_bar.update_btn.setEnabled(self._busy is None and any(e.get("download_url") for e in checked))
            self.selection_bar.show()
            self.selection_bar.raise_()
            self._position_selection_bar()
        else:
            self.selection_bar.hide()

    def _position_selection_bar(self):
        bar = self.selection_bar
        bar.adjustSize()
        holder = self.list_holder.rect()
        bar.move((holder.width() - bar.width()) // 2, holder.height() - bar.height() - 16)

    def _on_select_all_clicked(self):
        visible = self._visible_entries()
        target = not all(e.get("_is_checked") for e in visible)
        for entry in visible:
            entry["_is_checked"] = target
        self.view.viewport().update()
        self._refresh_selection_ui()

    def _set_all_checked(self, value: bool):
        for entry in self.model.items():
            entry["_is_checked"] = value
        self.view.viewport().update()
        self._refresh_selection_ui()

    def _toggle_current(self):
        entry = entry_at(self.view.currentIndex())
        if entry:
            entry["_is_checked"] = not entry.get("_is_checked")
            self.view.viewport().update()
            self._refresh_selection_ui()

    def _on_current_changed(self, current, previous):
        entry = entry_at(current)
        if not entry:
            self.details.hide()
        elif self.settings.get("details_on_select", True) or self.details.isVisible():
            self.details.set_entry(entry, self._icon_for_entry(entry))
            self.details.show()

    def _open_current_details(self):
        entry = entry_at(self.view.currentIndex())
        if entry:
            self.details.set_entry(entry, self._icon_for_entry(entry))
            self.details.show()

    # ================================================================== Вид списка

    def _view_mode(self) -> str:
        mode = self.settings.get("view_mode", VIEW_FLAT)
        if self.settings.get("per_profile_view", True):
            mode = self._profile().get("view_mode") or mode
        return mode if mode in VIEW_MODES else VIEW_FLAT

    def _apply_view_settings(self):
        """Переносит настройки вида в делегат и фильтр (без пересборки строк)."""
        tree = self._view_mode() == VIEW_TREE
        self.delegate.tree = tree
        self.delegate.compact = bool(self.settings.get("compact_rows", False))
        self.delegate.groups = bool(self.settings.get("group_by_status", True))
        self.delegate.columns = set(self.settings.get("columns") or ())
        self.delegate.abandoned_hint = bool(self.settings.get("abandoned_warning", True))
        self.details.show_abandoned = self.delegate.abandoned_hint
        # Умолчание раскрытия применяем только при смене настройки, чтобы не сбрасывать «Раскрыть все» из меню строки
        expand = bool(self.settings.get("expand_dependencies", False))
        if getattr(self, "_expand_setting", None) != expand:
            self._expand_setting = expand
            self.proxy.set_expand_all(expand)

    def _rebuild_view(self):
        """Перестраивает список под новые настройки вида, сохраняя открытый мод."""
        self._set_data(self.mods_data)
        self.view.relayout()  # Высота строк могла измениться (компактный режим, заголовки групп)
        self.view.viewport().update()

    def _expand_all(self, expanded: bool):
        self.proxy.set_expand_all(expanded)
        self._after_tree_change()

    def _set_expanded(self, entry: dict, expanded: bool):
        if not self.settings.get("animations", True) or not self.isVisible():
            self.proxy.set_expanded(entry, expanded)
            self._after_tree_change()
            return
        # Плавное раскрытие: зависимости появляются с нулевой высотой и «вырастают» (сворачивание — наоборот)
        key = entry_key(entry)
        previous = self._animations.pop(key, None)
        if previous:
            previous.stop()
        start = self.delegate.anim.get(key, 0.0 if expanded else 1.0)
        if expanded:
            self.delegate.anim[key] = start
            self.proxy.set_expanded(entry, True)
            self._after_tree_change()
        animation = QVariantAnimation(self)
        animation.setDuration(ANIMATION_MS)
        animation.setStartValue(float(start))
        animation.setEndValue(1.0 if expanded else 0.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic if expanded else QEasingCurve.Type.InCubic)

        def step(value):
            self.delegate.anim[key] = float(value)
            self.view.relayout()

        def done():
            self._animations.pop(key, None)
            self.delegate.anim.pop(key, None)
            if not expanded:
                self.proxy.set_expanded(entry, False)
                self._after_tree_change()
            self.view.relayout()

        animation.valueChanged.connect(step)
        animation.finished.connect(done)
        self._animations[key] = animation
        animation.start()

    def _expand_current(self, expand: bool):
        index = self.view.currentIndex()
        entry, meta = entry_at(index), meta_at(index)
        if not entry or self._view_mode() != VIEW_TREE:
            return
        if meta.get("children"):
            if self.proxy.is_expanded(entry) != expand:
                self._set_expanded(entry, expand)
        elif meta.get("depth") and not expand:
            # ← на зависимости — к её моду
            parent = self.model.entry(meta["parent_row"])
            parent_index = self.proxy.mapFromSource(self.model.index(meta["parent_row"]))
            if parent and parent_index.isValid():
                self.view.setCurrentIndex(parent_index)

    def _after_tree_change(self):
        current = self.view.currentIndex()
        self.proxy.sort(0)
        self._refresh_empty_state()
        self._refresh_selection_ui()
        if current.isValid():
            self.view.scrollTo(self.view.currentIndex())

    def _select_entry(self, entry: dict):
        key = entry_key(entry)
        for row in range(self.proxy.rowCount()):
            idx = self.proxy.index(row, 0)
            if entry_key(entry_at(idx)) == key:
                self.view.selectionModel().setCurrentIndex(idx, QItemSelectionModel.SelectionFlag.ClearAndSelect)
                self.view.scrollTo(idx)
                return
        # Зависимость внутри свёрнутого мода — раскрываем его
        for row in range(self.model.rowCount()):
            meta = self.model.meta(row)
            if meta.get("depth") and entry_key(self.model.entry(row)) == key:
                parent = self.model.entry(meta["parent_row"])
                if not self.proxy.is_expanded(parent):
                    self._set_expanded(parent, True)
                    self._select_entry(entry)
                    return
        # Мод скрыт фильтром — показываем все
        if self.proxy.category != CAT_ALL or self.proxy.query or self.proxy.tag:
            self._reset_filters()
            self._select_entry(entry)

    def _close_details(self):
        self.view.selectionModel().clearSelection()
        self.view.setCurrentIndex(self.proxy.index(-1, 0))
        self.details.entry = None
        self.details.hide()

    def _focus_search(self):
        self.sidebar.set_page(PAGE_MODS)
        self.stack.setCurrentIndex(PAGE_MODS)
        self.search.setFocus()
        self.search.selectAll()

    def _on_escape(self):
        if self.search.text():
            self.search.clear()
        elif self.details.isVisible():
            self._close_details()
        elif self._checked_entries():
            self._set_all_checked(False)

    def _show_entry_menu(self, entry: dict, pos):
        t = self.tokens
        menu = QMenu(self)
        if not self.details.isVisible() or self.details.entry is not entry:
            menu.addAction(make_action(menu, t, "info", "Подробнее",
                                       lambda: (self._select_entry(entry), self._open_current_details())))
        if entry.get("download_url"):
            verb = "Обновить до" if entry.get("filepath") else "Установить"
            menu.addAction(make_action(menu, t, "download", f"{verb} {entry.get('new_version', '')}",
                                       lambda: self.start_update([entry]), "accent"))
        if entry.get("project_id"):
            menu.addAction(make_action(menu, t, "history", "Выбрать другую версию…",
                                       lambda: (self._select_entry(entry), self._load_versions(entry))))
            menu.addAction(make_action(menu, t, "external", "Открыть на Modrinth", lambda: self._open_mod_page(entry)))
        if entry.get("filepath") and not self.is_import_mode:
            menu.addSeparator()
            pinned = bool(entry.get("_pinned"))
            menu.addAction(make_action(menu, t, "unlock" if pinned else "lock",
                                       "Разрешить обновления" if pinned else "Не обновлять этот мод (закрепить)",
                                       lambda: self._toggle_pin(entry)))
            menu.addAction(make_action(menu, t, "power", "Включить" if entry.get("disabled") else "Выключить (без удаления)",
                                       lambda: self._toggle_disabled([entry])))
        if entry.get("project_id") or entry.get("filepath"):
            menu.addAction(make_action(menu, t, "star_filled" if entry.get("_favorite") else "star",
                                       "Убрать из избранного" if entry.get("_favorite") else "В избранное",
                                       lambda: self._toggle_favorite([entry]), "warning" if entry.get("_favorite") else "text_muted"))
            menu.addAction(make_action(menu, t, "pencil", "Заметка…",
                                       lambda: (self._select_entry(entry), self._edit_note_current())))
            menu.addAction(make_action(menu, t, "tag", "Метки…", lambda: self._show_tags_menu(entry, pos)))
        if self._abandoned(entry):
            menu.addAction(make_action(menu, t, "compass", "Найти замену в каталоге", lambda: self._find_replacement(entry)))
        if self._view_mode() == VIEW_TREE:
            menu.addSeparator()
            menu.addAction(make_action(menu, t, "chevron_down", "Раскрыть все зависимости", lambda: self._expand_all(True)))
            menu.addAction(make_action(menu, t, "chevron_right", "Свернуть все", lambda: self._expand_all(False)))
            menu.addSeparator()
        if entry.get("filepath"):
            menu.addAction(make_action(menu, t, "folder", "Показать в папке", lambda: self._show_in_folder(entry)))
            menu.addSeparator()
            menu.addAction(make_action(menu, t, "trash", "Удалить", lambda: self._delete_entries([entry]), "danger"))
        if not menu.isEmpty():
            menu.exec(pos)

    # ================================================================== Меню «⋯»

    def _show_more_menu(self):
        t = self.tokens
        menu = QMenu(self)
        menu.addAction(make_action(menu, t, "import", "Импортировать строку сборки…", self.import_mod_list))
        menu.addAction(make_action(menu, t, "package", "Открыть .mrpack…", self.import_mrpack_dialog))
        menu.addAction(make_action(menu, t, "globe", "Синхронизация по URL…", self.sync_from_url))
        menu.addSeparator()
        has_data = bool(self.mods_data) and not self.is_import_mode and self.content is MODS
        migrate = make_action(menu, t, "trending_up", "Перейти на другую версию Minecraft…", self.migrate_game_version)
        migrate.setEnabled(has_data)
        menu.addAction(migrate)
        snapshot = make_action(menu, t, "camera", "Создать снимок сборки", lambda: self._create_snapshot("manual"))
        snapshot.setEnabled(bool(self.mods_data) and not self.is_import_mode)
        menu.addAction(snapshot)
        menu.addSeparator()
        tools = menu.addMenu(icons.icon("shield", t["text_muted"], 16), "Если игра не запускается")
        tools.addAction(make_action(tools, t, "bug", "Почему упала игра (разбор краша)…", self._show_crash_dialog))
        bisect = make_action(tools, t, "scissors", "Найти проблемный мод перебором…", self._start_bisect)
        bisect.setEnabled(has_data and not self._profile().get("bisect"))
        tools.addAction(bisect)
        tools.addAction(make_action(tools, t, "shield", "Проверить целостность файлов", self._check_integrity))
        menu.addAction(make_action(menu, t, "bar_chart", "Статистика сборки", lambda: self._switch_page(PAGE_STATS)))
        menu.addSeparator()
        for icon_name, text, slot in (("share", "Поделиться списком…", self.share_mod_list),
                                      ("save", "Экспорт JSON для сервера…", self.export_server_json),
                                      ("package", "Экспорт в .mrpack…", self.export_mrpack)):
            action = make_action(menu, t, icon_name, text, slot)
            action.setEnabled(has_data)
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(make_action(menu, t, "folder", f"Открыть папку {self.content.folder}",
                                   lambda: self._open_path(self.mods_path)))
        menu.addAction(make_action(menu, t, "refresh", "Перечитать папку", self._reread_folder))
        menu.exec(self.more_btn.mapToGlobal(self.more_btn.rect().bottomRight()) - self._menu_offset(menu))

    @staticmethod
    def _menu_offset(menu: QMenu):
        from PyQt6.QtCore import QPoint
        return QPoint(menu.sizeHint().width(), 0)

    def _reread_folder(self):
        self.sessions.pop(self._skey(), None)
        self.start_local_scan()

    # ================================================================== Фоновые операции

    def _set_busy(self, kind: Optional[str], text: str = "", determinate: bool = False):
        self._busy = kind
        self.progress_box.setVisible(kind is not None)
        self.status_label.setText(text)
        if not determinate:
            self.slim_progress.setRange(0, 0)
        self.sidebar.profile_list.setEnabled(kind is None)
        if self.taskbar:
            if kind is None:
                self.taskbar.clear()
            elif not determinate:
                self.taskbar.busy()
        self._refresh_actions()
        self._refresh_selection_ui()

    def start_local_scan(self):
        if self._busy or not self.mods_path:
            return
        if not os.path.isdir(self.mods_path):
            self._set_data([])
            return
        self._set_busy("local", f"Читаем папку {self.content.folder}…")
        self._refresh_empty_state()
        self.local_scan_worker = LocalScanWorker(self.mods_path, app_config.CACHE_FILE, self.content)
        self.local_scan_worker.finished.connect(self._on_local_scan_finished)
        self.local_scan_worker.start()

    def _on_local_scan_finished(self, mods_data):
        self.local_scan_worker = None
        self._set_busy(None)
        if mods_data is None:
            return
        self._set_data(mods_data)
        self.sessions[self._skey()] = mods_data
        if mods_data and self.settings.get("auto_check", True):
            self.start_scan(silent=True)

    def _on_check_clicked(self):
        if self._busy in ("scan", "local", "import"):
            worker = self.scan_worker or self.local_scan_worker or self.import_worker
            if worker:
                worker.stop()
                self.status_label.setText("Отмена…")
            return
        self.start_scan()

    def start_scan(self, silent: bool = False):
        if self._busy or not self.mods_path or not os.path.isdir(self.mods_path) or self.is_import_mode:
            return
        if not self.game_version or not self.loader:
            self.toasts.show("Не удалось определить версию игры или загрузчик — укажите их в настройках сборки.",
                             "warning", "Настроить", self._edit_current_profile)
            return
        self._silent_scan = silent
        channel = self._channel()
        logging.info(f"Проверка обновлений: Minecraft {self.game_version} ({self.loader}), канал {channel.value}")
        self._set_busy("scan", "Проверяем обновления…")
        self.scan_worker = ScanWorker(self.mods_path, self.game_version, self.loader, app_config.CACHE_FILE, channel,
                                      self.content)
        self.scan_worker.finished.connect(self._on_scan_finished)
        self.scan_worker.progress.connect(self.status_label.setText)
        self.scan_worker.start()

    def _on_scan_finished(self, mods_data):
        self.scan_worker = None
        self._set_busy(None)
        if mods_data is None:
            self.toasts.show("Проверка отменена.", "info")
            return
        if not mods_data and self.mods_data:
            self.toasts.show("Не удалось проверить обновления. Проверьте подключение к интернету.", "error")
            return
        fresh = self._mark_new_updates(mods_data)
        self._set_data(mods_data)
        self.sessions[self._skey()] = mods_data
        counts = category_counts(self._count_items)
        if fresh:
            names = ", ".join(e.get("name") for e in fresh[:3]) + (f" и ещё {len(fresh) - 3}" if len(fresh) > 3 else "")
            self.toasts.show(f"С прошлого запуска вышли новые версии: {names}", "info", "Показать",
                             lambda: (self._reset_filters(), self.search.setText("is:new")), timeout=10000)
        updates, problems = counts[CAT_UPDATES], counts[CAT_PROBLEMS]
        if updates:
            self.toasts.show(f"Доступно обновлений: {updates}", "info", "Обновить все", self.update_all, timeout=8000)
        elif not self._silent_scan:
            self.toasts.show("Все моды актуальны.", "success")
        if problems:
            self.toasts.show(f"Найдены проблемы с зависимостями: {problems}", "warning", "Показать",
                             lambda: self._set_category(CAT_PROBLEMS), timeout=8000)

    def _run_task(self, func, args, on_success: Callable, on_failure: Callable, busy_text: Optional[str]):
        """Разовая блокирующая операция в фоне. busy_text=None — не блокировать интерфейс (поиск в каталоге)."""
        worker = TaskWorker(func, *args)
        self._task_workers.append(worker)
        previous_busy = self._busy if busy_text is not None else "background"
        if previous_busy is None:
            self._set_busy("task", busy_text)

        def done():
            if worker in self._task_workers:
                self._task_workers.remove(worker)
            if previous_busy is None and self._busy == "task":
                self._set_busy(None)

        worker.succeeded.connect(lambda result: (done(), on_success(result)))
        worker.failed.connect(lambda error: (done(), on_failure(error)))
        worker.start()

    # ================================================================== Обновление и установка

    def update_all(self):
        entries = self._bulk_candidates()
        if not entries or self._busy:
            return
        if self.settings.get("changelog_before_update", True) and not self.is_import_mode:
            game_version, loader, content = self.game_version, self.loader, self.content
            dialog = UpdateReviewDialog(
                self, self.tokens, entries, self._icon_for_entry,
                lambda callback: self._run_task(collect_changelogs, (entries, game_version, loader, content),
                                                callback, lambda e: callback({}), None))
            if not dialog.exec():
                return
            entries = dialog.selected_entries()
            if not entries:
                return
        if not self.is_import_mode:
            self._auto_snapshot("update_all")
        self.start_update(entries)

    def _update_checked(self):
        self.start_update([e for e in self._checked_entries() if e.get("download_url")])

    def _confirm_game_not_running(self, paths: List[str]) -> bool:
        while True:
            locked = find_locked_files([p for p in paths if p])
            if not locked and not is_game_running(self.mods_path):
                return True
            names = "\n".join(f"• {os.path.basename(p)}" for p in locked[:5])
            text = ("Похоже, Minecraft запущен: файлы модов заблокированы.\n"
                    "Закройте игру, иначе файлы не удастся заменить.")
            if names:
                text += f"\n\nЗаблокированы:\n{names}"
            choice = ask_choice(self, QMessageBox.Icon.Warning, "Игра запущена?", text,
                                ["Проверить снова", "Продолжить всё равно", "Отмена"], default=0)
            if choice == 1:
                return True
            if choice != 0:
                return False

    def start_update(self, entries: List[dict], transaction: Optional[UpdateTransaction] = None,
                     folder: Optional[str] = None):
        """Скачивает и ставит entries в folder (по умолчанию — папка текущей вкладки)."""
        if self._busy:
            self.toasts.show("Дождитесь окончания текущей операции.", "warning")
            return
        folder = folder or self.mods_path
        unique: Dict[object, dict] = {}
        for entry in entries:
            if entry.get("download_url") and entry_key(entry) not in unique:
                unique[entry_key(entry)] = entry
        entries = list(unique.values())
        if not entries:
            return
        if not folder:
            show_message(self, QMessageBox.Icon.Warning, "Нет папки игры", "Сначала укажите папку игры в настройках сборки.")
            return
        os.makedirs(folder, exist_ok=True)
        if self.is_import_mode and transaction is None:
            proceed, transaction = self._ask_import_install_method()
            if not proceed:
                return
        if transaction is None and not self._confirm_game_not_running([e.get("filepath") for e in entries]):
            return

        self._update_done = 0
        self.slim_progress.setRange(0, len(entries))
        self.slim_progress.setValue(0)
        self._set_busy("update", f"Обновление: 0 из {len(entries)}", determinate=True)
        if self.taskbar:
            self.taskbar.set_value(0, len(entries))
        self._update_folder = folder
        self.update_worker = UpdateWorker(entries, folder, transaction,
                                          max_workers=self.settings.get("parallel_downloads", 3))
        self.update_worker.progress.connect(self._on_update_progress)
        self.update_worker.download_progress.connect(self._on_download_progress)
        self.update_worker.finished.connect(self._on_update_finished)
        self.update_worker.start()

    def _entries_like(self, entry: dict) -> List[dict]:
        key = entry_key(entry)
        return [e for e in self._all_entries() if entry_key(e) == key]

    def _on_update_progress(self, index: int, status_value: str):
        if not self.update_worker:
            return
        source = self.update_worker.mods_to_update[index]
        status = ModStatus(status_value)
        success = status in (ModStatus.UPDATED, ModStatus.INSTALLED)
        for entry in self._entries_like(source):
            entry["status"] = status
            entry.pop("_progress", None)
            if success:
                new_filename = source.get("new_filename")
                if new_filename and entry.get("disabled"):
                    new_filename += DISABLED_SUFFIX
                entry.update({
                    "current_version": source.get("new_version", ""), "new_version": "", "new_version_date": "",
                    "download_url": None, "new_filename": None, "new_hashes": None, "installed": True,
                    "filepath": os.path.join(self._update_folder, new_filename) if new_filename else entry.get("filepath"),
                    "size": source.get("new_size") or entry.get("size"), "new_size": 0,
                    "installed_info": source.get("_latest_info") or entry.get("installed_info"),
                    "_is_checked": False,
                })
        if status != ModStatus.DOWNLOADING:
            self._update_done += 1
            total = len(self.update_worker.mods_to_update)
            self.slim_progress.setValue(self._update_done)
            self.status_label.setText(f"Обновление: {self._update_done} из {total}")
            if self.taskbar:
                self.taskbar.set_value(self._update_done, total, error=status.is_error)
        self._refresh_project(source.get("project_id"))
        if self.details.entry and entry_key(self.details.entry) == entry_key(source):
            self.details.set_entry(self.details.entry, self._icon_for_entry(self.details.entry))

    def _on_download_progress(self, index: int, downloaded: int, total: int):
        if not self.update_worker or total <= 0:
            return
        source = self.update_worker.mods_to_update[index]
        for entry in self._entries_like(source):
            entry["_progress"] = downloaded / total
        self._refresh_project(source.get("project_id"))

    def _on_update_finished(self):
        worker = self.update_worker
        self.update_worker = None
        self._set_busy(None)
        if not worker:
            return
        results = worker.results
        failed = [worker.mods_to_update[i] for i, s in results.items() if s.is_error]
        succeeded = sum(1 for s in results.values() if s in (ModStatus.UPDATED, ModStatus.INSTALLED))
        transaction = worker.transaction
        if self._catalog_installing:
            self._finish_catalog_install(worker)
            return

        if self.is_import_mode and succeeded:
            self.is_import_mode = False
            self.banner.hide()
            self.sessions.pop(self._skey(), None)
            self.start_local_scan()
        else:
            self._set_data(self.mods_data)  # Пересчёт проблем и счётчиков
            self.sessions[self._skey()] = self.mods_data

        if succeeded:
            self._push_undo("обновление", lambda: self._rollback_dir(transaction.dir))
            self._prune_backups()
            self.toasts.show(f"Готово: {succeeded} {self._plural(succeeded, 'мод', 'мода', 'модов')} обновлено",
                             "success", "Отменить", lambda: self._rollback_dir(transaction.dir), timeout=9000)
        if failed:
            names = "\n".join(f"• {e.get('name')}: {e.get('status')}" for e in failed[:15])
            self.toasts.show(f"Не удалось обновить: {len(failed)}", "error", "Подробнее",
                             lambda: show_message(self, QMessageBox.Icon.Warning, "Ошибки обновления",
                                                  f"{names}\n\nПодробности на странице «Журнал»."), timeout=12000)

    @staticmethod
    def _plural(n: int, one: str, few: str, many: str) -> str:
        if n % 10 == 1 and n % 100 != 11:
            return one
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return few
        return many

    def _ask_import_install_method(self):
        choice = ask_choice(
            self, QMessageBox.Icon.Question, "Как установить сборку?",
            "<b>Заменить все:</b> текущие моды уйдут в бэкап, будет установлена только новая сборка.<br><br>"
            "<b>Объединить:</b> моды из сборки добавятся к текущим, остальные останутся.<br><br>"
            "<span style='opacity:0.7'>Любой вариант можно отменить на странице «История».</span>",
            ["Заменить все", "Объединить", "Отмена"], default=0, rich_text=True)
        if choice not in (0, 1):
            return False, None
        jars = [os.path.join(self.mods_path, f) for f in os.listdir(self.mods_path) if f.lower().endswith(".jar")]
        if not self._confirm_game_not_running(jars):
            return False, None
        transaction = UpdateTransaction(self.mods_path, "replace_all" if choice == 0 else "update")
        if choice == 1:
            # Объединение: если мод из сборки уже установлен, новая версия заменяет старый файл, а не ложится рядом
            installed = {e["project_id"]: e["filepath"]
                         for e in iter_all_entries(self.sessions.get(self._skey(content=MODS)) or [])
                         if e.get("project_id") and e.get("filepath") and os.path.exists(e["filepath"])}
            for entry in self.model.items():
                if not entry.get("filepath") and entry.get("project_id") in installed:
                    entry["filepath"] = installed[entry["project_id"]]
        if choice == 0:
            self._auto_snapshot("replace_all")
            try:
                move_all_to_backup(self.mods_path, transaction)
            except Exception as e:
                logging.error(f"Ошибка при переносе модов в бэкап: {e}")
                show_message(self, QMessageBox.Icon.Critical, "Ошибка бэкапа",
                             f"Не удалось перенести моды в бэкап: {e}\nУже перенесённые файлы можно вернуть в «Истории».")
                return False, None
        return True, transaction

    def _load_versions(self, entry: dict):
        if not entry.get("project_id"):
            return
        self._run_task(fetch_versions, (entry["project_id"], self.game_version, self.loader),
                       lambda versions: self.details.entry is entry and self.details.set_versions(versions),
                       lambda e: self.toasts.show(f"Не удалось загрузить версии: {e}", "error"),
                       "Загружаем список версий…")

    def _install_specific_version(self, entry: dict, version: dict):
        primary = select_primary_file(version)
        if not primary:
            return
        entry.update({
            "new_version": version.get("version_number", ""), "new_version_date": format_date(version.get("date_published")),
            "download_url": primary["url"], "new_filename": primary["filename"], "new_hashes": primary.get("hashes"),
            "_latest_info": version,
        })
        if not entry.get("filepath"):
            entry["status"] = ModStatus.READY_TO_INSTALL
        self.start_update([entry])

    # ================================================================== Удаление и откат

    def _delete_checked(self):
        self._delete_entries([e for e in self._checked_entries() if e.get("filepath")])

    def _delete_checked_or_current(self):
        checked = [e for e in self._checked_entries() if e.get("filepath")]
        if checked:
            self._delete_entries(checked)
        else:
            entry = entry_at(self.view.currentIndex())
            if entry and entry.get("filepath"):
                self._delete_entries([entry])

    def _delete_entries(self, entries: List[dict]):
        paths = list(dict.fromkeys(e["filepath"] for e in entries if e.get("filepath")))
        if not paths or self._busy:
            return
        what = f"«{entries[0].get('name')}»" if len(paths) == 1 else f"{len(paths)} модов"
        if not confirm(self, "Удаление", f"Удалить {what}?\nФайлы уйдут в бэкап — удаление можно отменить.",
                       "Удалить", "Отмена"):
            return
        if not self._confirm_game_not_running(paths):
            return
        transaction = UpdateTransaction(self.mods_path, "delete")
        removed, errors = remove_mods(paths, transaction)
        removed_set = {os.path.normcase(os.path.normpath(p)) for p in removed}

        def keep(entry):
            fp = entry.get("filepath")
            return not (fp and os.path.normcase(os.path.normpath(fp)) in removed_set)

        mods_data = [m for m in self.mods_data if keep(m)]
        for mod in mods_data:
            if mod.get("dependencies"):
                mod["dependencies"] = [d for d in mod["dependencies"] if keep(d)]
        self._set_data(mods_data)
        self.sessions[self._skey()] = mods_data
        if removed:
            self._push_undo("удаление", lambda: self._rollback_dir(transaction.dir))
            self.toasts.show(f"Удалено: {len(removed)}", "success", "Отменить",
                             lambda: self._rollback_dir(transaction.dir), timeout=9000)
        if errors:
            show_message(self, QMessageBox.Icon.Critical, "Ошибка удаления", "\n".join(errors[:10]))

    def _rollback_dir(self, tx_dir: str):
        # <папка>/ModUpdater_Backups/transactions/<время>: операция могла быть в другой папке (установка из каталога)
        folder = os.path.dirname(os.path.dirname(os.path.dirname(tx_dir)))
        manifest = next((m for m in list_transactions(folder) if os.path.normcase(m["dir"]) == os.path.normcase(tx_dir)), None)
        if not manifest:
            self.toasts.show("Эта операция уже отменена.", "info")
            return
        self._do_rollback(manifest, folder)

    def _rollback_from_history(self, manifest: dict):
        transactions = list_transactions(self.mods_path)
        is_latest = bool(transactions) and transactions[0]["dir"] == manifest["dir"]
        warning = "" if is_latest else ("\n\n⚠ Это не последняя операция: более поздние изменения тех же файлов "
                                         "могут конфликтовать — такие файлы будут пропущены.")
        if confirm(self, "Вернуть изменения", "Удалить файлы, установленные этой операцией, и вернуть прежние?"
                   + warning, "Вернуть", "Отмена"):
            self._do_rollback(manifest)

    def _do_rollback(self, manifest: dict, folder: Optional[str] = None):
        if self._busy:
            self.toasts.show("Дождитесь окончания текущей операции.", "warning")
            return
        folder = folder or self.mods_path
        paths = [os.path.join(folder, f) for f in manifest.get("installed", [])]
        if not self._confirm_game_not_running(paths):
            return
        errors = rollback_transaction(folder, manifest)
        if errors:
            show_message(self, QMessageBox.Icon.Warning, "Откат выполнен частично", "\n".join(errors[:10]))
        else:
            self.toasts.show("Изменения отменены.", "success")
        self.is_import_mode = False
        self.banner.hide()
        self._invalidate_folder(folder)
        self._refresh_history()
        if os.path.normcase(folder) == os.path.normcase(self.mods_path):
            self.start_local_scan()

    # ================================================================== Импорт

    def import_mod_list(self):
        dialog = ImportDialog(self)
        if dialog.exec():
            self._import_text(dialog.get_share_string())

    def _import_text(self, text: str):
        text = (text or "").strip()
        if not text:
            return
        if text.lower().startswith(("http://", "https://")):
            self._sync_url(text)
            return
        try:
            self._process_imported_data(decode_share_string(text))
        except ValueError as e:
            show_message(self, QMessageBox.Icon.Critical, "Ошибка импорта", f"Не удалось прочитать строку: {e}")

    def sync_from_url(self):
        dialog = SyncDialog(self)
        if dialog.exec() and dialog.get_url():
            self._sync_url(dialog.get_url())

    def _sync_url(self, url: str):
        if not url.lower().startswith(("http://", "https://")):
            show_message(self, QMessageBox.Icon.Warning, "Ошибка", "Укажите адрес, начинающийся с http:// или https://")
            return
        self._run_task(fetch_json, (url,), self._process_imported_data,
                       lambda e: show_message(self, QMessageBox.Icon.Critical, "Ошибка", f"Не удалось загрузить данные:\n{e}"),
                       "Загружаем сборку…")

    def _process_imported_data(self, share_data):
        try:
            request = parse_share_data(share_data)
        except ValueError as e:
            show_message(self, QMessageBox.Icon.Critical, "Ошибка", f"Не удалось прочитать данные:\n{e}")
            return
        use_exact = False
        if request.version >= 2:
            strict_text = ("Автор закрепил точные версии модов." if request.strict
                           else "Автор не закреплял версии.")
            choice = ask_choice(self, QMessageBox.Icon.Question, "Импорт сборки",
                                f"Minecraft {request.game_version} ({request.loader}), модов: {len(request.project_ids)}.\n"
                                f"{strict_text}\n\nКакие версии установить?",
                                ["Точные версии", "Самые новые", "Отмена"], default=0 if request.strict else 1)
            if choice not in (0, 1):
                return
            use_exact = choice == 0
        elif not confirm(self, "Импорт сборки",
                         f"Импортировать {len(request.project_ids)} модов для Minecraft {request.game_version} ({request.loader})?"):
            return
        if self._busy:
            return
        self._set_busy("import", "Ищем совместимые версии…")
        self._import_meta = (request.game_version, request.loader)
        self.import_worker = ImportWorker(request.target_mods, request.game_version, request.loader, use_exact,
                                          self._channel())
        self.import_worker.finished.connect(self._on_import_finished)
        self.import_worker.progress.connect(self.status_label.setText)
        self.import_worker.start()

    def _on_import_finished(self, entries):
        self.import_worker = None
        self._set_busy(None)
        if entries is None:
            self.toasts.show("Импорт отменён.", "info")
            return
        if not entries:
            self.toasts.show("Не удалось найти совместимые версии модов.", "error")
            return
        game_version, loader = self._import_meta
        self._enter_import_mode(entries, game_version, loader)

    def import_mrpack_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Открыть сборку", "", "Modrinth Modpack (*.mrpack)")
        if path:
            self._import_mrpack(path)

    def _import_mrpack(self, path: str):
        try:
            index = read_mrpack(path)
        except ValueError as e:
            show_message(self, QMessageBox.Icon.Critical, "Ошибка", str(e))
            return
        game_version, loader = mrpack_game_info(index)
        name = index.get("name") or os.path.basename(path)
        self._run_task(entries_from_mrpack, (index,),
                       lambda entries: self._enter_import_mode(entries, game_version, loader, name),
                       lambda e: show_message(self, QMessageBox.Icon.Critical, "Ошибка", f"Не удалось прочитать сборку:\n{e}"),
                       "Читаем сборку…")

    def _enter_import_mode(self, entries: List[dict], game_version: Optional[str], loader: Optional[str],
                           name: Optional[str] = None):
        if not entries:
            self.toasts.show("В сборке не найдено модов для установки.", "warning")
            return
        if self.mods_data and not self.is_import_mode:
            self.sessions[self._skey()] = self.mods_data
        self.is_import_mode = True
        title = f"«{name}»" if name else "из списка"
        text = f"<b>Импорт сборки {title}</b> — {len(entries)} модов, Minecraft {game_version or '?'} ({loader or '?'})."
        if (game_version and game_version != self.game_version) or (loader and loader != self.loader):
            text += (f"<br><span style='color:{self.tokens['warning']}'>⚠ У этой сборки другая версия: "
                     f"у вас Minecraft {self.game_version} ({self.loader}).</span>")
        self.banner_text.setText(text)
        self.banner.show()
        self._close_details()
        self._set_data(entries, keep_selection=False)
        self._set_category(CAT_ALL)

    def _exit_import_mode(self):
        self.is_import_mode = False
        self.banner.hide()
        cached = self.sessions.get(self._skey(content=MODS))
        if cached is not None:
            self._set_data(cached)
        else:
            self._set_data([])
            self.start_local_scan()

    # ================================================================== Экспорт

    def _ask_strict(self, recipient: str) -> Optional[bool]:
        choice = ask_choice(self, QMessageBox.Icon.Question, "Настройки экспорта",
                            f"Закрепить текущие версии модов?\n\nДа — {recipient} точно такие же версии.\n"
                            f"Нет — {recipient} самые новые версии.", ["Точные версии", "Всегда новые", "Отмена"], default=1)
        return None if choice not in (0, 1) else choice == 0

    def share_mod_list(self):
        share_data = build_share_data(self.mods_data, self.game_version, self.loader, strict=False)
        if not share_data["mods"]:
            self.toasts.show("Нет модов с Modrinth — сначала проверьте обновления.", "warning")
            return
        strict = self._ask_strict("получатель установит")
        if strict is None:
            return
        share_data["strict"] = strict
        ShareDialog(encode_share_string(share_data), self).exec()

    def export_server_json(self):
        share_data = build_share_data(self.mods_data, self.game_version, self.loader, strict=False)
        if not share_data["mods"]:
            self.toasts.show("Нет модов с Modrinth — сначала проверьте обновления.", "warning")
            return
        strict = self._ask_strict("клиенты скачают")
        if strict is None:
            return
        share_data["strict"] = strict
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить JSON для сервера", "modpack.json", "JSON (*.json)")
        if path:
            try:
                save_server_json(path, share_data)
                self.toasts.show(f"Файл сохранён: {os.path.basename(path)}", "success")
            except OSError as e:
                show_message(self, QMessageBox.Icon.Critical, "Ошибка", f"Не удалось сохранить файл:\n{e}")

    def export_mrpack(self):
        loader_version = self.current_loader_version or self.latest_loader_version
        if not loader_version:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                loader_version = get_latest_loader_version(self.loader, self.game_version)
            finally:
                QApplication.restoreOverrideCursor()
        if not loader_version:
            show_message(self, QMessageBox.Icon.Warning, "Ошибка",
                         f"Не удалось определить версию загрузчика {self.loader} для {self.game_version}.")
            return
        index = build_mrpack_index(self.mods_data, self.game_version, self.loader, loader_version,
                                   name=self.current_profile_name)
        if not index["files"]:
            self.toasts.show("Нет модов с Modrinth — сначала проверьте обновления.", "warning")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить .mrpack", f"{self.current_profile_name}.mrpack",
                                              "Modrinth Modpack (*.mrpack)")
        if not path:
            return
        try:
            write_mrpack(path, index)
            self.toasts.show(f"Сборка ({len(index['files'])} модов) сохранена в {os.path.basename(path)}", "success")
        except OSError as e:
            show_message(self, QMessageBox.Icon.Critical, "Ошибка", f"Не удалось сохранить файл:\n{e}")

    # ================================================================== Перетаскивание

    @staticmethod
    def _dropped_files(mime) -> List[str]:
        return [u.toLocalFile() for u in mime.urls() if u.isLocalFile()] if mime.hasUrls() else []

    def dragEnterEvent(self, event):
        files = self._dropped_files(event.mimeData())
        ok = any(f.lower().endswith((".jar", ".mrpack", ".zip")) for f in files) or (not files and event.mimeData().hasText())
        if ok and not self._busy:
            event.acceptProposedAction()
            self.drop_overlay.setGeometry(self.centralWidget().rect().adjusted(262, 12, -12, -12))
            self.drop_overlay.show()
            self.drop_overlay.raise_()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.drop_overlay.hide()

    def dropEvent(self, event):
        self.drop_overlay.hide()
        files = self._dropped_files(event.mimeData())
        event.acceptProposedAction()
        self.sidebar.set_page(PAGE_MODS)
        self.stack.setCurrentIndex(PAGE_MODS)
        packs = [f for f in files if f.lower().endswith(".mrpack")]
        jars = [f for f in files if f.lower().endswith(".jar")]
        zips = [f for f in files if f.lower().endswith(".zip")]
        if packs:
            self._import_mrpack(packs[0])
        elif jars:
            if self.content is not MODS:
                self._set_content(MODS)
            self._add_jars(jars)
        elif zips:
            if self.content is MODS:
                self.toasts.show("Архивы .zip — это шейдеры или ресурспаки: откройте нужную вкладку и перетащите снова.",
                                 "info", timeout=7000)
                return
            self._add_jars(zips)
        elif event.mimeData().hasText():
            self._import_text(event.mimeData().text())

    def _add_jars(self, paths: List[str]):
        if not self.mods_path:
            self.toasts.show("Сначала укажите папку игры для этой сборки.", "warning")
            return
        transaction = UpdateTransaction(self.mods_path, "add")
        added, errors = add_files(paths, self.mods_path, transaction)
        if added:
            self.toasts.show(f"Добавлено файлов: {len(added)}", "success", "Отменить",
                             lambda: self._rollback_dir(transaction.dir), timeout=9000)
            self.sessions.pop(self._skey(), None)
            self.start_local_scan()
        if errors:
            show_message(self, QMessageBox.Icon.Warning, "Не все файлы добавлены", "\n".join(errors[:10]))

    # ================================================================== Прочее

    def _open_path(self, path: str):
        if path and os.path.exists(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        else:
            self.toasts.show("Папка не найдена.", "warning")

    def _show_in_folder(self, entry: dict):
        path = entry.get("filepath")
        if not path or not os.path.exists(path):
            return
        if os.name == "nt":
            import subprocess
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            self._open_path(os.path.dirname(path))

    def _open_mod_page(self, entry: dict):
        if entry.get("project_id"):
            kind = {"shader": "shader", "resourcepack": "resourcepack"}.get(entry.get("project_type"), "mod")
            webbrowser.open(f"https://modrinth.com/{kind}/{entry.get('slug') or entry['project_id']}")

    def _open_catalog_hit(self, hit: Optional[dict]):
        if hit:
            webbrowser.open(f"https://modrinth.com/{hit.get('project_type', 'mod')}/{hit.get('slug') or hit.get('project_id')}")

    # ================================================================== Закрепление, выключение, метки

    def _same_file_entries(self, path: str) -> List[dict]:
        norm = os.path.normcase(os.path.normpath(path))
        return [e for e in self._all_entries() if e.get("filepath") and os.path.normcase(os.path.normpath(e["filepath"])) == norm]

    def _toggle_pin(self, entry: dict):
        key = pin_key(entry)
        if not key:
            return
        pinned = self._profile().setdefault("pinned", {})
        self._push_undo("закрепление", lambda: self._toggle_pin(entry))
        if key in pinned:
            del pinned[key]
            self.toasts.show(f"«{entry.get('name')}» снова обновляется.", "info")
        else:
            pinned[key] = entry.get("current_version") or ""
            self.toasts.show(f"«{entry.get('name')}» закреплён: обновления для него не предлагаются.", "success",
                             "Отменить", lambda: self._toggle_pin(entry))
        self._set_data(self.mods_data)
        self.save_config()

    def _toggle_disabled(self, entries: List[dict]):
        entries = [e for e in entries if e.get("filepath")]
        if not entries or self._busy:
            return
        disable = not all(e.get("disabled") for e in entries)
        if not self._confirm_game_not_running([e["filepath"] for e in entries]):
            return
        changed, errors = [], []
        for entry in entries:
            if bool(entry.get("disabled")) == disable:
                continue
            old_path = entry["filepath"]
            try:
                new_path = set_disabled(old_path, disable)
            except OSError as e:
                errors.append(f"{entry.get('name')}: {e}")
                continue
            for same in self._same_file_entries(old_path):
                same["filepath"], same["disabled"] = new_path, disable
                same["_is_checked"] = False
            changed.append(entry)
        if changed:
            self._push_undo("выключение" if disable else "включение", lambda: self._toggle_disabled(changed))
            self._set_data(self.mods_data)
            self.sessions[self._skey()] = self.mods_data
            what = f"«{changed[0].get('name')}»" if len(changed) == 1 else f"{len(changed)} модов"
            self.toasts.show(f"{'Выключено' if disable else 'Включено'}: {what}", "success", "Отменить",
                             lambda: self._toggle_disabled(changed), timeout=8000)
        if errors:
            show_message(self, QMessageBox.Icon.Warning, "Не удалось переименовать", "\n".join(errors[:10]))

    def _show_tags_menu(self, entry: dict, pos):
        key = pin_key(entry)
        if not key:
            return
        t = self.tokens
        menu = QMenu(self)
        menu.addAction(make_action(menu, t, "plus", "Добавить метку…", lambda: self._add_tag(entry)))
        own = self.mod_tags.get(key, [])
        if own:
            menu.addSeparator()
        for tag in own:
            menu.addAction(make_action(menu, t, "x", f"Убрать «{tag}»", lambda _=False, tg=tag: self._remove_tag(entry, tg)))
        menu.exec(pos)

    def _add_tag(self, entry: dict):
        existing = sorted({tag for tags in self.mod_tags.values() for tag in tags})
        hint = "Например: интерфейс, для сервера, графика" + (f"\nУже есть: {', '.join(existing[:8])}" if existing else "")
        tag = ask_text(self, "Новая метка", hint, "Название метки")
        if not tag:
            return
        tags = self.mod_tags.setdefault(pin_key(entry), [])
        if tag.lower() not in (t.lower() for t in tags):
            tags.append(tag)
            self._push_undo(f"метка «{tag}»", lambda: self._remove_tag(entry, tag))
        self._after_tags_changed(entry)

    def _remove_tag(self, entry: dict, tag: str):
        key = pin_key(entry)
        tags = [t for t in self.mod_tags.get(key, []) if t != tag]
        if tags:
            self.mod_tags[key] = tags
        else:
            self.mod_tags.pop(key, None)
        self._after_tags_changed(entry)

    def _after_tags_changed(self, entry: dict):
        self._refresh_tag_combo()
        self.proxy.invalidateFilter()
        self._refresh_empty_state()
        if self.details.entry is entry:
            self.details.set_entry(entry, self._icon_for_entry(entry))
        self.save_config()

    # ================================================================== Снимки сборки

    def _auto_snapshot(self, reason: str):
        if self.settings.get("auto_snapshot", True) and self.mods_path:
            self._create_snapshot(reason, quiet=True)

    def _create_snapshot(self, reason: str = "manual", quiet: bool = False):
        if not self.mods_path or not os.path.isdir(self.mods_path):
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            snapshot = create_snapshot(self.mods_path, reason, self.content)
            if reason != "manual":
                prune_snapshots(self.mods_path, int(self.settings.get("snapshots_keep", 10)))
        except OSError as e:
            logging.error(f"Не удалось создать снимок: {e}")
            snapshot = None
            if not quiet:
                show_message(self, QMessageBox.Icon.Warning, "Снимок не создан", str(e))
        finally:
            QApplication.restoreOverrideCursor()
        if snapshot and not quiet:
            self.toasts.show(f"Снимок сохранён: {len(snapshot['files'])} файлов", "success", "История",
                             lambda: self._switch_page(PAGE_HISTORY))
        if self.stack.currentIndex() == PAGE_HISTORY:
            self._refresh_history()

    def _restore_snapshot(self, snapshot: dict):
        if self._busy:
            self.toasts.show("Дождитесь окончания текущей операции.", "warning")
            return
        count = len(snapshot.get("files") or [])
        if not confirm(self, "Восстановить снимок",
                       f"Вернуть папку {self.content.folder} к снимку от {snapshot.get('created', '').replace('T', ' ')} "
                       f"({count} файлов)?\nТекущие файлы уйдут в бэкап — восстановление можно отменить.",
                       "Восстановить", "Отмена"):
            return
        files = [os.path.join(self.mods_path, f) for f in os.listdir(self.mods_path) if self.content.matches(f)]
        if not self._confirm_game_not_running(files):
            return
        transaction, errors = restore_snapshot(self.mods_path, snapshot, self.content)
        if errors:
            show_message(self, QMessageBox.Icon.Warning, "Восстановлено частично", "\n".join(errors[:10]))
        else:
            self.toasts.show("Сборка восстановлена из снимка.", "success", "Отменить",
                             lambda: self._rollback_dir(transaction.dir), timeout=9000)
        self.sessions.pop(self._skey(), None)
        self._refresh_history()
        self.start_local_scan()

    def _delete_snapshot(self, snapshot: dict):
        if confirm(self, "Удалить снимок", "Удалить этот снимок? Файлы текущей сборки не изменятся.", "Удалить", "Отмена"):
            delete_snapshot(snapshot)
            self._refresh_history()

    def _invalidate_folder(self, folder: str):
        """Забывает сохранённые списки, если их папка изменилась."""
        game_dir = self._profile().get("mods_path", "")
        for content in CONTENT_TYPES:
            if os.path.normcase(content.path(game_dir)) == os.path.normcase(folder or ""):
                self.sessions.pop(self._skey(content=content), None)

    # ================================================================== Переход на другую версию Minecraft

    def migrate_game_version(self):
        if self._busy or not self.mods_data or self.content is not MODS or self.is_import_mode:
            return
        entries = [e for e in self._count_items if e.get("filepath")]
        dialog = MigrationDialog(self, self.tokens, entries, self.game_version, self.loader, self._channel(),
                                 self._run_task, self.content)
        if not dialog.exec() or not dialog.results:
            return
        target = dialog.target
        ready = [r for r in dialog.results if r["version"]]
        missing = [r["entry"] for r in dialog.results if not r["version"]]
        if not self._confirm_game_not_running([e.get("filepath") for e in entries]):
            return
        self._auto_snapshot("migrate")
        # Версия, которая уже стоит и подходит под новую версию игры, не скачивается повторно
        to_install = [r["entry"] for r in ready
                      if r["version"].get("id") != (r["entry"].get("installed_info") or {}).get("id")
                      and apply_install_info(r["entry"], r["version"])]
        if dialog.disable_missing.isChecked():
            for entry in missing:
                if entry.get("disabled"):
                    continue
                old_path = entry["filepath"]
                try:
                    new_path = set_disabled(old_path, True)
                except OSError as e:
                    logging.error(f"Не удалось выключить {entry.get('name')}: {e}")
                    continue
                for same in self._same_file_entries(old_path):
                    same["filepath"], same["disabled"] = new_path, True
        old_version = self.game_version
        self._profile()["game_version"] = target
        self.game_version = target
        self._update_header()
        self._refresh_sidebar()
        self.start_update(to_install, UpdateTransaction(self.mods_path, "migrate"))
        show_message(self, QMessageBox.Icon.Information, "Не забудьте про лаунчер",
                     f"Моды обновляются под Minecraft {target}.\n\nСмените версию игры и загрузчика на {target} "
                     f"в лаунчере (в Prism: «Изменить» → «Версия»). Вернуть всё как было можно на странице «История» — "
                     f"там сохранён снимок сборки под {old_version}.")

    # ================================================================== Каталог

    def _installed_ids(self) -> Dict[str, set]:
        result: Dict[str, set] = {}
        for content in CONTENT_TYPES:
            data = self.mods_data if content is self.content else self.sessions.get(self._skey(content=content)) or []
            result[content.key] = {e["project_id"] for e in iter_all_entries(data) if e.get("project_id") and e.get("filepath")}
        return result

    def _refresh_catalog_context(self):
        self.catalog_page.set_context(self.game_version, self.loader, self._installed_ids())
        # Типы контента, которые ещё не открывались: читаем их папки, чтобы отметить уже установленное
        game_dir = self._profile().get("mods_path", "")
        for content in CONTENT_TYPES:
            key = self._skey(content=content)
            if content is not self.content and key not in self.sessions and os.path.isdir(content.path(game_dir)):
                self._run_task(scan_local, (content.path(game_dir), app_config.CACHE_FILE, content),
                               lambda data, k=key: self._on_catalog_folder_read(k, data), lambda e: None, None)

    def _on_catalog_folder_read(self, key: tuple, data: List[dict]):
        self.sessions.setdefault(key, data)
        self.catalog_page.set_context(self.game_version, self.loader, self._installed_ids())

    def _install_from_catalog(self, hit: dict):
        if self._busy:
            self.toasts.show("Дождитесь окончания текущей операции.", "warning")
            return
        content = self.catalog_page.content
        if not self._profile().get("mods_path"):
            self.toasts.show("Сначала укажите папку игры для этой сборки.", "warning", "Настроить", self._edit_current_profile)
            return
        pid = hit.get("project_id")
        installed = self._installed_ids().get(content.key, set())
        self.catalog_page.set_installing([pid], True)
        self._run_task(resolve_project_install,
                       (pid, self.game_version, self.loader, self._channel(), installed, content),
                       lambda entries: self._on_catalog_resolved(hit, content, entries),
                       lambda e: self._on_catalog_failed(pid, e), None)

    def _on_catalog_failed(self, pid: str, error: str):
        self.catalog_page.set_installing([pid], False)
        self.toasts.show(f"Не удалось подготовить установку: {error}", "error")

    def _on_catalog_resolved(self, hit: dict, content: ContentType, entries: List[dict]):
        pid = hit.get("project_id")
        self.catalog_page.set_installing([pid], False)
        if not entries:
            self.toasts.show(f"У «{hit.get('title')}» нет версии для Minecraft {self.game_version}"
                             + (f" ({self.loader})" if content is MODS else "") + ".", "warning")
            return
        if len(entries) > 1:
            deps = ", ".join(e.get("name") for e in entries[1:])
            self.toasts.show(f"Вместе с «{entries[0].get('name')}» будут установлены зависимости: {deps}", "info")
        folder = content.path(self._profile().get("mods_path", ""))
        self._catalog_installing = [e["project_id"] for e in entries]
        self._catalog_content = content
        self.catalog_page.set_installing(self._catalog_installing, True)
        self.start_update(entries, folder=folder)
        if not self.update_worker:  # Установка не началась (игра запущена или отмена)
            self.catalog_page.set_installing(self._catalog_installing, False)
            self._catalog_installing = []

    def _finish_catalog_install(self, worker):
        installing, self._catalog_installing = self._catalog_installing, []
        self.catalog_page.set_installing(installing, False)
        results = worker.results.items()
        succeeded = [worker.mods_to_update[i] for i, s in results if s in (ModStatus.UPDATED, ModStatus.INSTALLED)]
        failed = [worker.mods_to_update[i] for i, s in worker.results.items() if s.is_error]
        self._invalidate_folder(worker.mods_path)
        installed = self._installed_ids()
        installed.setdefault(self._catalog_content.key, set()).update(e["project_id"] for e in succeeded)
        self.catalog_page.set_context(self.game_version, self.loader, installed)
        if succeeded:
            names = ", ".join(e.get("name") for e in succeeded[:3]) + (" и др." if len(succeeded) > 3 else "")
            self.toasts.show(f"Установлено: {names}", "success", "Отменить",
                             lambda: self._rollback_dir(worker.transaction.dir), timeout=9000)
        if failed:
            self.toasts.show(f"Не удалось установить: {', '.join(e.get('name') for e in failed)}", "error")
        if os.path.normcase(worker.mods_path) == os.path.normcase(self.mods_path) and not self.is_import_mode:
            self.start_local_scan()

    # ================================================================== Быстрые команды (Ctrl+K)

    def _palette_commands(self) -> List[Command]:
        commands = [
            Command("Проверить обновления", self._on_check_clicked, "refresh", hint="F5"),
            Command("Обновить все", self.update_all, "download", keywords="установить"),
            Command("Найти моды в каталоге", self._open_catalog_search, "compass", keywords="modrinth поиск скачать новые"),
            Command("Создать снимок сборки", lambda: self._create_snapshot("manual"), "camera", keywords="бэкап резерв"),
            Command("Перейти на другую версию Minecraft", self.migrate_game_version, "trending_up",
                    keywords="миграция обновить версию игры"),
            Command("Открыть .mrpack", self.import_mrpack_dialog, "package", keywords="импорт сборка"),
            Command("Импортировать строку сборки", self.import_mod_list, "import"),
            Command("Поделиться списком модов", self.share_mod_list, "share", keywords="экспорт"),
            Command("Открыть папку", lambda: self._open_path(self.mods_path), "folder"),
            Command("Перечитать папку", self._reread_folder, "refresh"),
            Command("Настройки сборки", self._edit_current_profile, "pencil"),
            Command("Добавить сборку", self._add_profile, "plus"),
            Command("Найти сборки в лаунчерах", self._discover_now, "box_search", keywords="prism modrinth curseforge"),
            Command("Почему упала игра (разбор краша)", self._show_crash_dialog, "bug", keywords="краш вылет crash"),
            Command("Найти проблемный мод перебором", self._start_bisect, "scissors", keywords="бисекция вылет краш"),
            Command("Проверить целостность файлов", self._check_integrity, "shield", keywords="битые"),
            Command("Отменить последнее действие", self._undo_last, "undo", hint="Ctrl+Z"),
            Command("Клонировать сборку", lambda: self._clone_profile(self.current_profile_name), "copy"),
            Command("Экспорт настроек", self._export_settings, "share", group="Данные"),
            Command("Импорт настроек", self._import_settings, "import", group="Данные"),
            Command("Очистить старые бэкапы", lambda: self._prune_backups(manual=True), "trash", group="Данные"),
            Command("Показать избранные", lambda: (self._switch_page(PAGE_MODS), self.search.setText("is:fav")), "star",
                    group="Фильтры"),
            Command("Показать давно не обновлявшиеся", lambda: (self._switch_page(PAGE_MODS), self.search.setText("is:abandoned")),
                    "alert", group="Фильтры"),
            Command("Показать новые обновления", lambda: (self._switch_page(PAGE_MODS), self.search.setText("is:new")),
                    "sparkles", group="Фильтры"),
            Command("Режим «Минимум»: вкл/выкл", self._toggle_minimal, "list", group="Оформление"),
            Command("Тёмная тема", lambda: self._set_theme_from_palette("dark"), "moon", group="Оформление"),
            Command("Светлая тема", lambda: self._set_theme_from_palette("light"), "sun", group="Оформление"),
            Command("Тема как в системе", lambda: self._set_theme_from_palette("system"), "settings", group="Оформление"),
        ]
        for mode, (icon_name, title) in {"flat": ("list", "Все моды и зависимости"), "mine": ("user", "Только мои моды"),
                                         "tree": ("list_tree", "Дерево с зависимостями")}.items():
            commands.append(Command(f"Вид: {title}", lambda m=mode: self._set_view_mode(m), icon_name, group="Вид списка"))
        for page, (icon_name, title) in enumerate(Sidebar.PAGES):
            commands.append(Command(f"Открыть: {title}", lambda p=page: self._switch_page(p), icon_name, group="Страницы"))
        for content in CONTENT_TYPES:
            commands.append(Command(f"Вкладка: {content.title}", lambda c=content: self._open_content_tab(c),
                                    CONTENT_ICONS[content.key], group="Страницы"))
        for name, profile in self.profiles.items():
            commands.append(Command(name, lambda n=name: self._open_profile_from_palette(n), "layers", group="Сборка",
                                    hint=profile_subtitle(profile)))
        for name in self._presets():
            commands.append(Command(f"Набор: {name}", lambda n=name: self._apply_preset(n), "layers", group="Наборы модов"))
        for entry in self._count_items:
            commands.append(Command(entry.get("name") or "?", lambda e=entry: self._palette_open_entry(e),
                                    CONTENT_ICONS[self.content.key], group=self.content.title_one.capitalize(),
                                    hint=entry.get("current_version") or "", keywords=entry.get("slug") or ""))
        return commands

    def _open_catalog_search(self):
        self._switch_page(PAGE_CATALOG)
        self.catalog_page.search.setFocus()

    def _open_content_tab(self, content: ContentType):
        self._switch_page(PAGE_MODS)
        self._set_content(content)

    def _open_profile_from_palette(self, name: str):
        self._switch_page(PAGE_MODS)
        self._on_profile_selected(name)
        self._refresh_sidebar()

    def _palette_open_entry(self, entry: dict):
        self._switch_page(PAGE_MODS)
        self._select_entry(entry)
        self._open_current_details()

    def _set_theme_from_palette(self, theme: str):
        self._on_theme_selected(theme)
        self.settings_page.load(self.config_theme, self.settings)

    def _set_view_mode(self, mode: str):
        self.settings["view_mode"] = mode
        self.settings_page.load(self.config_theme, self.settings)
        self._switch_page(PAGE_MODS)
        self._rebuild_view()

    def _toggle_minimal(self):
        self.settings["minimal_mode"] = not self.settings.get("minimal_mode", False)
        self.settings_page.load(self.config_theme, self.settings)
        self._apply_minimal()
