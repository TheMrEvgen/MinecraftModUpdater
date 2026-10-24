"""Главное окно приложения: боковая панель со сборками, список модов, панель деталей и страницы."""
import logging
import os
import sys
import time
from typing import Dict, List, Optional

from PyQt6.QtCore import QByteArray, QEvent, QObject, QTimer, QVariantAnimation, Qt, pyqtSignal
from PyQt6.QtGui import QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QMainWindow, QMenu, QProgressBar,
    QStackedWidget, QVBoxLayout, QWidget,
)

import config as app_config
import icons
from content import CONTENT_TYPES, ContentType, MODS
from i18n import tr
from launchers import discover_instances
from taskbar import TaskbarProgress
from theme import apply_window_frame_theme, build_stylesheet, detect_system_theme, make_tokens
from ui.catalog import CatalogPage
from ui.details_panel import DetailsPanel
from ui.mod_list import ModDelegate, ModFilterProxy, ModListModel, ModListView
from ui.pages import HistoryPage, LogPage, SettingsPage
from ui.palette import CommandPalette
from ui.stats_page import StatsPage
from ui.tools_dialogs import BisectBanner
from ui.widgets import (
    ChipButton, DropOverlay, ElidedLabel, EmptyState, SearchBox, SelectionBar, Sidebar, StatCard, ToastManager,
    button, label, make_action, refresh_button_icons, style_segments,
)
from ui_components import confirm
from updater import backup_root, list_snapshots, list_transactions
from view_model import CAT_ALL, CAT_DISABLED, CAT_INSTALL, CAT_NOT_FOUND, CAT_PROBLEMS, CAT_UPDATES
from window.app import AppMixin
from window.catalog import CatalogMixin
from window.common import CONTENT_ICONS, PAGE_CATALOG, PAGE_HISTORY, PAGE_LOG, PAGE_MODS, PAGE_SETTINGS, PAGE_STATS
from window.game import GameMixin
from window.maintenance import MaintenanceMixin
from window.mod_actions import ModActionsMixin
from window.mod_list import ModListMixin
from window.modpack import ModpackMixin
from window.operations import OperationsMixin
from window.palette import PaletteMixin
from window.preferences import PreferencesMixin
from window.profiles import ProfilesMixin
from window.transfer import TransferMixin
from window.tray import TrayMixin
from workers import IconLoader, ImportWorker, LoaderVersionWorker, LocalScanWorker, ScanWorker, TaskWorker, UpdateWorker


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


class ModUpdaterApp(ProfilesMixin, ModListMixin, OperationsMixin, TransferMixin, ModpackMixin, CatalogMixin,
                    PaletteMixin, ModActionsMixin, GameMixin, MaintenanceMixin, AppMixin, PreferencesMixin, TrayMixin,
                    QMainWindow):
    """Главное окно. Методы по темам — в примесях window/*.py, здесь — интерфейс, тема, страницы и закрытие."""
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
        self._game_running = False
        self._launched_at: Optional[float] = None      # Когда игру запустили кнопкой «Играть»
        self._game_started_at: Optional[float] = None
        self._auto_bisect: Optional[dict] = None       # Состояние автоматического поиска проблемного мода
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
        QTimer.singleShot(6000, self._check_app_update)
        self._game_timer = QTimer(self, interval=2000)  # Наблюдение за игрой: запущена ли, не вылетела ли
        self._game_timer.timeout.connect(self._poll_game)
        self._game_timer.start()
        self._setup_tray()

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
        self.sidebar.collapse_toggled.connect(self._on_sidebar_toggled)
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
        self.settings_page.app_update_check_requested.connect(lambda: self._check_app_update(manual=True))
        self.settings_page.app_update_open_requested.connect(self._open_app_update)
        self.settings_page.autostart_toggled.connect(self._set_autostart)
        self.settings_page.background_check_requested.connect(lambda: self._start_background_check(manual=True))
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
        self.edit_btn = button("", "IconButton", "pencil", t, "text_muted", tr("Настройки сборки"))
        self.edit_btn.clicked.connect(self._edit_current_profile)
        header.addWidget(self.edit_btn)
        header.addStretch()
        self.play_btn = button(tr("Играть"), "PlayButton", "play", t, "accent", tr("Запустить сборку"))
        self.play_btn.clicked.connect(self._play)
        self.play_btn.setMinimumHeight(36)
        header.addWidget(self.play_btn)
        self.check_btn = button(tr("Проверить"), None, "refresh", t, "text", tr("Проверить обновления (F5)"))
        self.check_btn.clicked.connect(self._on_check_clicked)
        self.update_all_btn = button(tr("Обновить все"), "PrimaryButton", "download", t, "accent_text")
        self.update_all_btn.clicked.connect(self.update_all)
        self.more_btn = button("", "IconButton", "more", t, "text", tr("Импорт, экспорт и другое"))
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
        self.preset_combo.setToolTip(tr("Наборы модов: переключение включает и выключает нужные моды"))
        self.preset_combo.activated.connect(lambda i: self.preset_combo.itemData(i) and self._apply_preset(self.preset_combo.itemData(i)))
        self.preset_btn = button(tr(" Наборы"), None, "layers", t, "text_muted", tr("Сохранить или применить набор модов"))
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
        self.world_chip = ChipButton()  # Только на вкладке «Датапаки»
        self.world_chip.clicked.connect(self._show_world_menu)
        self.world_chip.hide()
        for chip in (self.game_chip, self.loader_chip, self.channel_chip, self.world_chip):
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
        self.banner_cancel = button(tr("Отменить импорт"), "GhostButton", "x", t, "text_muted")
        self.banner_cancel.clicked.connect(self._exit_import_mode)
        banner_layout.addWidget(self.banner_icon)
        banner_layout.addWidget(self.banner_text, 1)
        banner_layout.addWidget(self.banner_cancel)
        self.banner.hide()
        layout.addWidget(self.banner)

        # --- Игра запущена ---
        self.game_banner = QFrame()
        self.game_banner.setObjectName("Banner")
        game_row = QHBoxLayout(self.game_banner)
        game_row.setContentsMargins(14, 8, 8, 8)
        self.game_icon = QLabel()
        game_row.addWidget(self.game_icon)
        game_row.addWidget(label(tr("<b>Игра запущена</b> — изменения модов заблокированы, пока она открыта."), None, True), 1)
        stop = button(tr("Закрыть игру"), None, "square", t, "text")
        stop.clicked.connect(self._stop_game)
        game_row.addWidget(stop)
        self.game_banner.hide()
        layout.addWidget(self.game_banner)

        # --- Вышла новая версия Minecraft ---
        self.readiness_banner = QFrame()
        self.readiness_banner.setObjectName("Banner")
        readiness = QHBoxLayout(self.readiness_banner)
        readiness.setContentsMargins(14, 8, 8, 8)
        self.readiness_icon = QLabel()
        self.readiness_text = label("", None, wrap=True)
        self.readiness_text.setTextFormat(Qt.TextFormat.RichText)
        self.readiness_more = button(tr("Подробнее"), None, "trending_up", t, "text")
        self.readiness_more.clicked.connect(lambda: self.migrate_game_version(self._profile().get("readiness", {}).get("target")))
        self.readiness_close = button("", "IconButton", "x", t, "text_muted", tr("Скрыть до следующей версии"))
        self.readiness_close.setFixedSize(30, 30)
        self.readiness_close.clicked.connect(self._dismiss_readiness)
        readiness.addWidget(self.readiness_icon)
        readiness.addWidget(self.readiness_text, 1)
        readiness.addWidget(self.readiness_more)
        readiness.addWidget(self.readiness_close)
        self.readiness_banner.hide()
        layout.addWidget(self.readiness_banner)

        # --- Поиск проблемного мода (шаги) ---
        self.bisect_banner = BisectBanner(t)
        self.bisect_banner.answered.connect(self._on_bisect_answer)
        self.bisect_banner.cancel_requested.connect(self._cancel_bisect)
        self.bisect_banner.finish_requested.connect(self._finish_bisect)
        self.bisect_banner.auto_stop_requested.connect(self._stop_auto_bisect)
        layout.addWidget(self.bisect_banner)

        # --- Карточки-сводки (фильтры) ---
        cards = QHBoxLayout()
        cards.setSpacing(10)
        self.card_group = QButtonGroup(self)
        self.card_group.setExclusive(True)
        self.cards: Dict[str, StatCard] = {}
        for category, title, color in ((CAT_ALL, tr("Все моды"), "text"), (CAT_UPDATES, tr("Обновления"), "accent"),
                                       (CAT_INSTALL, tr("К установке"), "info"), (CAT_PROBLEMS, tr("Проблемы"), "warning"),
                                       (CAT_NOT_FOUND, tr("Нет в каталогах"), "text_muted"), (CAT_DISABLED, tr("Выключены"), "text_muted")):
            card = StatCard(title, category, color, t)
            card.clicked.connect(lambda _=False, c=category: self._set_category(c))
            self.card_group.addButton(card)
            self.cards[category] = card
            cards.addWidget(card)
        cards.addStretch()  # Счётчики — по ширине содержимого, прижаты влево
        self.cards[CAT_ALL].setChecked(True)
        self.cards_widget = QWidget()
        self.cards_widget.setLayout(cards)
        cards.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.cards_widget)

        # --- Поиск и сортировка ---
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.search = SearchBox(t, tr("Поиск: название, файл, заметка или условие — tag:, side:client, is:fav, size>5mb"))
        self.search.setMinimumHeight(36)
        self._search_timer = QTimer(self, singleShot=True, interval=150)
        self._search_timer.timeout.connect(lambda: self._apply_filter(query=self.search.text()))
        self.search.textChanged.connect(lambda: self._search_timer.start())
        toolbar.addWidget(self.search, 1)
        self.select_all = QCheckBox(tr("Выбрать все"))
        self.select_all.setTristate(True)
        self.select_all.clicked.connect(self._on_select_all_clicked)
        toolbar.addWidget(self.select_all)
        self.sort_combo = QComboBox()
        self.tag_combo = QComboBox()
        self.tag_combo.setMinimumHeight(36)
        self.tag_combo.setToolTip(tr("Фильтр по метке: категории Modrinth, сторона (клиент/сервер) и ваши метки"))
        self.tag_combo.currentIndexChanged.connect(self._on_tag_selected)
        toolbar.addWidget(self.tag_combo)
        self.sort_combo.addItem(tr("Сначала важные"), ModFilterProxy.SORT_STATUS)
        self.sort_combo.addItem(tr("По названию"), ModFilterProxy.SORT_NAME)
        self.sort_combo.addItem(tr("Недавно обновлённые"), ModFilterProxy.SORT_UPDATED)
        self.sort_combo.addItem(tr("По размеру"), ModFilterProxy.SORT_SIZE)
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
        self.readiness_icon.setPixmap(icons.pixmap("trending_up", t["info"], 18))
        self.game_icon.setPixmap(icons.pixmap("play", t["accent"], 18))
        self.sidebar.set_background(self.settings.get("sidebar_image") or "")
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
            suffix = f"{t['name']}_{t['accent_text'].lstrip('#')}.svg"
            check = os.path.join(directory, "check_" + suffix)
            chevron = os.path.join(directory, "chevron_" + suffix)  # Таблица стилей берёт путь галочки с заменой check → chevron
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
        self._apply_sidebar_width()
        self.drop_overlay.setGeometry(self.centralWidget().rect().adjusted(self.sidebar.width() + 10, 12, -12, -12))
        from ui.details_panel import panel_width
        self.details.setFixedWidth(panel_width(self.stack.width() - 56))
        if self.palette.isVisible():
            self.palette.open()

    AUTO_COLLAPSE_WIDTH = 1150  # Уже — боковая панель сворачивается сама

    def _apply_sidebar_width(self):
        if not hasattr(self, "sidebar"):
            return
        self.sidebar.set_collapsed(bool(self.settings.get("sidebar_collapsed")) or self.width() < self.AUTO_COLLAPSE_WIDTH)

    def _on_sidebar_toggled(self, collapsed: bool):
        if not collapsed and self.width() < self.AUTO_COLLAPSE_WIDTH:
            self.toasts.show(tr("Окно слишком узкое — панель развернётся, когда окно станет шире."), "info")
        self.settings["sidebar_collapsed"] = collapsed
        self.save_config()
        self._apply_sidebar_width()

    def closeEvent(self, event):
        if self._hide_to_tray():  # «Оставаться в трее»: окно прячется, программа работает дальше
            event.ignore()
            return
        workers = [w for w in (self.scan_worker, self.import_worker, self.local_scan_worker, self.update_worker,
                               self.loader_version_worker, self._bg_worker, *self._task_workers) if w and w.isRunning()]
        if self.update_worker and self.update_worker.isRunning():
            if not confirm(self, tr("Идёт обновление"), tr("Моды ещё скачиваются. Прервать и закрыть программу?"),
                           tr("Прервать и закрыть"), tr("Продолжить")):
                self._quitting = False
                event.ignore()
                return
        updating = bool(self.update_worker and self.update_worker.isRunning())
        if self.tray:
            self.tray.hide()
        for worker in workers:
            if hasattr(worker, "stop"):
                worker.stop()
        # Сначала сохраняем и прячем окно: ожидание потоков не должно выглядеть как зависание
        self.window_cfg["geometry"] = bytes(self.saveGeometry().toBase64()).decode()
        self.save_config()
        self.hide()
        QApplication.processEvents()
        loaders = (self.icon_loader, self.thumb_loader, self.full_loader)
        for loader in loaders:
            loader.shutdown(0)
        # Поток с сетевым запросом останавливается только после ответа (а Modrinth может отвечать минуту).
        # Обновлению даём время откатить начатое, остальным — пару секунд
        deadline = time.monotonic() + (20 if updating else 2)
        for worker in workers:
            worker.wait(max(0, int((deadline - time.monotonic()) * 1000)))
        stuck = [type(w).__name__ for w in workers if w.isRunning()]
        stuck += [type(l).__name__ for l in loaders if l.pool.activeThreadCount()]
        logging.getLogger().removeHandler(self._gui_log_handler)
        super().closeEvent(event)
        QApplication.instance().quit()  # Выход из трея и обычное закрытие: окна больше нет — завершаемся
        if stuck:
            # Ждать дальше бессмысленно: настройки сохранены, а прерывать потоки Qt не умеет
            logging.warning(tr('Закрытие без ожидания фоновых задач: {0}', ', '.join(stuck)))
            logging.shutdown()
            for stream in (sys.stdout, sys.stderr):
                try:
                    stream.flush()
                except (AttributeError, OSError, ValueError):  # В .exe без консоли потоков может не быть
                    pass
            os._exit(0)

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
            self._refresh_app_update_ui()
            self._refresh_background_ui()

    def _refresh_history(self):
        if not self.mods_path:
            self.history_page.set_transactions([], [])
            self.history_page.usage_label.setText("")
            return
        self.history_page.set_transactions(list_transactions(self.mods_path), list_snapshots(self.mods_path))
        self.history_page.usage_label.setText(self._backup_usage_text())

    # ================================================================== Меню «⋯»

    def _show_more_menu(self):
        t = self.tokens
        menu = QMenu(self)
        menu.addAction(make_action(menu, t, "import", tr("Импортировать строку сборки…"), self.import_mod_list))
        menu.addAction(make_action(menu, t, "package", tr("Открыть .mrpack…"), self.import_mrpack_dialog))
        menu.addAction(make_action(menu, t, "globe", tr("Синхронизация по URL…"), self.sync_from_url))
        menu.addSeparator()
        has_data = bool(self.mods_data) and not self.is_import_mode and self.content is MODS
        migrate = make_action(menu, t, "trending_up", tr("Перейти на другую версию Minecraft…"), self.migrate_game_version)
        migrate.setEnabled(has_data)
        menu.addAction(migrate)
        snapshot = make_action(menu, t, "camera", tr("Создать снимок сборки"), lambda: self._create_snapshot("manual"))
        snapshot.setEnabled(bool(self.mods_data) and not self.is_import_mode)
        menu.addAction(snapshot)
        menu.addSeparator()
        tools = menu.addMenu(icons.icon("shield", t["text_muted"], 16), tr("Если игра не запускается"))
        tools.addAction(make_action(tools, t, "bug", tr("Почему упала игра (разбор краша)…"), self._show_crash_dialog))
        bisect = make_action(tools, t, "scissors", tr("Найти проблемный мод перебором…"), self._start_bisect)
        bisect.setEnabled(has_data and not self._profile().get("bisect"))
        tools.addAction(bisect)
        tools.addAction(make_action(tools, t, "shield", tr("Проверить целостность файлов"), self._check_integrity))
        menu.addAction(make_action(menu, t, "bar_chart", tr("Статистика сборки"), lambda: self._switch_page(PAGE_STATS)))
        menu.addSeparator()
        for icon_name, text, slot in (("share", tr("Поделиться списком…"), self.share_mod_list),
                                      ("save", tr("Экспорт JSON для сервера…"), self.export_server_json),
                                      ("package", tr("Экспорт в .mrpack…"), self.export_mrpack)):
            action = make_action(menu, t, icon_name, text, slot)
            action.setEnabled(has_data)
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(make_action(menu, t, "folder", tr('Открыть папку {0}', self.content.folder),
                                   lambda: self._open_path(self.mods_path)))
        menu.addAction(make_action(menu, t, "refresh", tr("Перечитать папку"), self._reread_folder))
        menu.exec(self.more_btn.mapToGlobal(self.more_btn.rect().bottomRight()) - self._menu_offset(menu))

    @staticmethod
    def _menu_offset(menu: QMenu):
        from PyQt6.QtCore import QPoint
        return QPoint(menu.sizeHint().width(), 0)

    def _reread_folder(self):
        self.sessions.pop(self._skey(), None)
        self.start_local_scan()

