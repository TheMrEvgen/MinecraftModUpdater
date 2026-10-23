"""Быстрые команды (Ctrl+K).

Примесь к главному окну (ModUpdaterApp): методы работают с его атрибутами."""
from typing import List

from content import CONTENT_TYPES, ContentType
from i18n import tr
from ui.palette import Command
from ui.widgets import Sidebar, profile_subtitle
from window.common import CONTENT_ICONS, PAGE_CATALOG, PAGE_MODS


class PaletteMixin:
    # ================================================================== Быстрые команды (Ctrl+K)

    def _palette_commands(self) -> List[Command]:
        commands = [
            Command(tr("Проверить обновления"), self._on_check_clicked, "refresh", hint="F5"),
            Command(tr("Обновить все"), self.update_all, "download", keywords=tr("установить")),
            Command(tr("Найти моды в каталоге"), self._open_catalog_search, "compass", keywords=tr("modrinth поиск скачать новые")),
            Command(tr("Создать снимок сборки"), lambda: self._create_snapshot("manual"), "camera", keywords=tr("бэкап резерв")),
            Command(tr("Перейти на другую версию Minecraft"), self.migrate_game_version, "trending_up",
                    keywords=tr("миграция обновить версию игры")),
            Command(tr("Открыть .mrpack"), self.import_mrpack_dialog, "package", keywords=tr("импорт сборка")),
            Command(tr("Импортировать строку сборки"), self.import_mod_list, "import"),
            Command(tr("Поделиться списком модов"), self.share_mod_list, "share", keywords=tr("экспорт")),
            Command(tr("Открыть папку"), lambda: self._open_path(self.mods_path), "folder"),
            Command(tr("Перечитать папку"), self._reread_folder, "refresh"),
            Command(tr("Настройки сборки"), self._edit_current_profile, "pencil"),
            Command(tr("Добавить сборку"), self._add_profile, "plus"),
            Command(tr("Найти сборки в лаунчерах"), self._discover_now, "box_search", keywords="prism modrinth curseforge"),
            Command(tr("Почему упала игра (разбор краша)"), self._show_crash_dialog, "bug", keywords=tr("краш вылет crash")),
            Command(tr("Найти проблемный мод перебором"), self._start_bisect, "scissors", keywords=tr("бисекция вылет краш")),
            Command(tr("Проверить целостность файлов"), self._check_integrity, "shield", keywords=tr("битые")),
            Command(tr("Отменить последнее действие"), self._undo_last, "undo", hint="Ctrl+Z"),
            Command(tr("Клонировать сборку"), lambda: self._clone_profile(self.current_profile_name), "copy"),
            Command(tr("Экспорт настроек"), self._export_settings, "share", group=tr("Данные")),
            Command(tr("Импорт настроек"), self._import_settings, "import", group=tr("Данные")),
            Command(tr("Очистить старые бэкапы"), lambda: self._prune_backups(manual=True), "trash", group=tr("Данные")),
            Command(tr("Показать избранные"), lambda: (self._switch_page(PAGE_MODS), self.search.setText("is:fav")), "star",
                    group=tr("Фильтры")),
            Command(tr("Показать давно не обновлявшиеся"), lambda: (self._switch_page(PAGE_MODS), self.search.setText("is:abandoned")),
                    "alert", group=tr("Фильтры")),
            Command(tr("Показать новые обновления"), lambda: (self._switch_page(PAGE_MODS), self.search.setText("is:new")),
                    "sparkles", group=tr("Фильтры")),
            Command(tr("Режим «Минимум»: вкл/выкл"), self._toggle_minimal, "list", group=tr("Оформление")),
            Command(tr("Тёмная тема"), lambda: self._set_theme_from_palette("dark"), "moon", group=tr("Оформление")),
            Command(tr("Светлая тема"), lambda: self._set_theme_from_palette("light"), "sun", group=tr("Оформление")),
            Command(tr("Тема OLED (чёрная)"), lambda: self._set_theme_from_palette("oled"), "moon", group=tr("Оформление")),
            Command(tr("Контрастная тема"), lambda: self._set_theme_from_palette("contrast"), "sun", group=tr("Оформление")),
            Command(tr("Тема как в системе"), lambda: self._set_theme_from_palette("system"), "settings", group=tr("Оформление")),
        ]
        for mode, (icon_name, title) in {"flat": ("list", tr("Все моды и зависимости")), "mine": ("user", tr("Только мои моды")),
                                         "tree": ("list_tree", tr("Дерево с зависимостями"))}.items():
            commands.append(Command(tr('Вид: {0}', title), lambda m=mode: self._set_view_mode(m), icon_name, group=tr("Вид списка")))
        for page, (icon_name, title) in enumerate(Sidebar.PAGES):
            commands.append(Command(tr('Открыть: {0}', title), lambda p=page: self._switch_page(p), icon_name, group=tr("Страницы")))
        for content in CONTENT_TYPES:
            commands.append(Command(tr('Вкладка: {0}', content.title), lambda c=content: self._open_content_tab(c),
                                    CONTENT_ICONS[content.key], group=tr("Страницы")))
        for name, profile in self.profiles.items():
            commands.append(Command(name, lambda n=name: self._open_profile_from_palette(n), "layers", group=tr("Сборка"),
                                    hint=profile_subtitle(profile)))
        for name in self._presets():
            commands.append(Command(tr('Набор: {0}', name), lambda n=name: self._apply_preset(n), "layers", group=tr("Наборы модов")))
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
