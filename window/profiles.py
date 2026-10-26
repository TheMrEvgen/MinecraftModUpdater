"""Сборки: конфигурация, список слева, версии игры и загрузчика, порядок, группы, клонирование.

Примесь к главному окну (ModUpdaterApp): методы работают с его атрибутами."""
import logging
import os
import shutil
from typing import List, Optional

from PyQt6.QtWidgets import QMenu, QMessageBox

import config as app_config
import i18n
import icons
import modpack_install
from content import CONTENT_TYPES, ContentType, MODS, list_worlds, pick_world
from enums import UpdateChannel
from game_detection import detect_game_versions, extract_loader_version
from i18n import tr
from launchers import discover_instances
from maintenance import clone_game_dir, compare_entries
from scanner import scan_local
from ui.dialogs import ask_text
from ui.tools_dialogs import CloneDialog, CompareDialog
from ui.widgets import make_action, profile_subtitle, style_segments
from ui_components import ProfileDialog, confirm, show_message
from view_model import CAT_ALL, iter_all_entries
from window.common import LOADER_TITLES, PAGE_MODS
from workers import LoaderVersionWorker


class ProfilesMixin:
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
            logging.error(tr('Ошибка при сохранении конфигурации: {0}', e))

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
                                    if k in ("game_version", "loader", "loader_version", "launcher_id") and v})
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
                          "tooltip": profile.get("mods_path") or tr("Папка не указана"), "group": profile.get("group") or "",
                          "color": profile.get("color") or "", "icon": profile.get("icon") or ""})
        self.sidebar.set_profiles(items, self.current_profile_name)

    def _on_profile_selected(self, name: str):
        if name == self.current_profile_name:
            return
        if self._auto_bisect:
            self.toasts.show(tr("Идёт автоматический поиск проблемного мода — остановите его, чтобы сменить сборку."), "warning")
            self._refresh_sidebar()
            return
        if self._busy:
            self.toasts.show(tr("Дождитесь окончания текущей операции."), "warning")
            self._refresh_sidebar()
            return
        self._load_profile(name)

    def _skey(self, profile: Optional[str] = None, content: Optional[ContentType] = None) -> tuple:
        content = content or self.content
        name = profile or self.current_profile_name
        if content.per_world:  # Датапаки у каждого мира свои
            return name, f"{content.key}:{self._datapack_world(name) or ''}"
        return name, content.key

    def _datapack_world(self, profile_name: Optional[str] = None) -> Optional[str]:
        """Мир для вкладки «Датапаки»: выбранный в сборке или последний сыгранный."""
        profile = self.profiles.get(profile_name or self.current_profile_name) or {}
        return pick_world(profile.get("mods_path", ""), profile.get("datapack_world"))

    def _content_folder(self, content: Optional[ContentType] = None) -> str:
        content = content or self.content
        return content.path(self._profile().get("mods_path", ""), self._datapack_world() if content.per_world else None)

    def _show_world_menu(self):
        from PyQt6.QtWidgets import QMenu
        worlds = list_worlds(self._profile().get("mods_path", ""))
        menu = QMenu(self)
        current = self._datapack_world()
        for world in worlds:
            action = menu.addAction(("✓  " if world == current else "     ") + world)
            action.triggered.connect(lambda _=False, w=world: self._set_datapack_world(w))
        if not worlds:
            menu.addAction(tr("Миров пока нет")).setEnabled(False)
        menu.exec(self.world_chip.mapToGlobal(self.world_chip.rect().bottomLeft()))

    def _set_datapack_world(self, world: str):
        self._remember_session()
        self._profile()["datapack_world"] = world
        self.save_config()
        self._load_content()

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
            self.toasts.show(tr("Дождитесь окончания текущей операции."), "warning")
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
        self.mods_path = self._content_folder()
        self._loaded_key = self._skey()
        self._update_header()
        self.cards[CAT_ALL].title = tr('Все {0}', self.content.title.lower())
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
        self._show_readiness()
        self._check_modpack_update()
        self._game_running = False
        self.game_banner.hide()
        self._refresh_play_button()

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
        self.game_chip.setToolTip(tr("Изменить версию игры и загрузчик"))
        loader_title = LOADER_TITLES.get(self.loader, self.loader or tr("Загрузчик ?"))
        version = f" {self.current_loader_version}" if self.current_loader_version else ""
        has_update = bool(self.latest_loader_version and self.current_loader_version
                          and self._is_newer(self.latest_loader_version, self.current_loader_version))
        self.loader_chip.setText(f"{loader_title}{version}" + (f"  →  {self.latest_loader_version}" if has_update else ""))
        self.loader_chip.set_variant(warning=has_update)
        self.loader_chip.setToolTip(tr("Доступна новая версия загрузчика — обновите её в лаунчере") if has_update
                                    else tr("Версия загрузчика"))
        self.channel_chip.setText(f"{self._channel().display_name}  ▾")
        self.channel_chip.setToolTip(tr("Какие версии модов предлагать: только релизы, с бетами или все"))
        self.path_label.setFullText(profile.get("mods_path") or tr("Папка не указана"))
        world = self._datapack_world() if self.content.per_world else None
        self.world_chip.setVisible(self.content.per_world)
        self.world_chip.setText(tr("Мир: {0}  ▾", world) if world else tr("Миров нет"))
        self.world_chip.setToolTip(tr("Датапаки у каждого мира свои — выберите мир"))
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
            if self._launcher_target():
                self._update_loader_in_launcher()
            else:
                self.toasts.show(tr('Доступен {0} {1} (у вас {2}). Загрузчик обновляется в лаунчере.', LOADER_TITLES.get(self.loader, self.loader), self.latest_loader_version, self.current_loader_version), "info", timeout=8000)
        else:
            self._edit_current_profile()

    # --- Сборка Prism Launcher / MultiMC: версии меняем в самом лаунчере ---

    def _launcher_pack(self) -> Optional[str]:
        import launcher_pack
        return launcher_pack.pack_file(self._profile().get("mods_path", ""))

    def _launcher_target(self):
        """Где программа сама меняет версии игры и загрузчика: сборка Prism/MultiMC или установка Minecraft Launcher
        с Fabric/Quilt. None — только вручную в лаунчере."""
        import launcher_pack
        import official_launcher
        pack = self._launcher_pack()
        if pack:
            return launcher_pack.LauncherTarget("pack", pack, game_version=self.game_version)
        profile = self._profile()
        if profile.get("source") == "official" and profile.get("launcher_id") and official_launcher.can_install(self.loader):
            return launcher_pack.LauncherTarget("official", official_launcher.minecraft_dir(), profile["launcher_id"],
                                                self.game_version)
        return None

    def _launcher_title(self) -> str:
        return {"prism": "Prism Launcher", "multimc": "MultiMC", "official": "Minecraft Launcher"}.get(
            self._profile().get("source"), "Prism Launcher")

    def _launcher_warning(self) -> str:
        import launcher_pack
        import official_launcher
        running = (official_launcher.launcher_running() if self._profile().get("source") == "official"
                   else launcher_pack.launcher_running())
        return (tr("\n\n⚠ {0} сейчас открыт — закройте его, иначе при сохранении сборки он может вернуть прежние версии.",
                   self._launcher_title()) if running else "")

    def _update_loader_in_launcher(self):
        import launcher_pack
        loader, loader_title = self.loader, LOADER_TITLES.get(self.loader, self.loader)
        old, new = self.current_loader_version, self.latest_loader_version
        if not confirm(self, tr("Обновить загрузчик"),
                       tr("Обновить {0} {1} → {2} в сборке {3}?\n\nФайл сборки лаунчера сохраняется — изменение можно "
                          "отменить.", loader_title, old, new, self._launcher_title()) + self._launcher_warning(),
                       tr("Обновить"), tr("Отмена")):
            return
        target = self._launcher_target()

        def done(backup):
            self._after_launcher_change(game_version=None, loader_version=new)
            self.toasts.show(tr("{0} обновлён до {1} в {2}.", loader_title, new, self._launcher_title()), "success",
                             tr("Отменить"), lambda: self._undo_launcher_change(target, backup, None, old), timeout=12000)

        self._run_task(launcher_pack.change_versions, (target, self.game_version, loader, new), done,
                       lambda e: show_message(self, QMessageBox.Icon.Critical, tr("Не удалось обновить загрузчик"), e),
                       tr("Обновляем загрузчик…"))

    def _after_launcher_change(self, game_version: Optional[str], loader_version: Optional[str]):
        profile = self._profile()
        if game_version:
            profile["game_version"] = game_version
            self.game_version = game_version
        if loader_version:
            profile["loader_version"] = loader_version
            self.current_loader_version = loader_version
            self.latest_loader_version = None
        self.save_config()
        self._update_header()
        self._refresh_sidebar()
        self._start_loader_check()

    def _undo_launcher_change(self, target, backup: str, game_version: Optional[str], loader_version: Optional[str]):
        import launcher_pack
        try:
            launcher_pack.undo(target, backup)
        except OSError as e:
            show_message(self, QMessageBox.Icon.Critical, tr("Не удалось вернуть"), str(e))
            return
        self._after_launcher_change(game_version, loader_version)
        self.toasts.show(tr("Версии в сборке лаунчера возвращены."), "info")

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
        menu.addAction(make_action(menu, t, "folder", tr("Указать папку игры…"), self._add_profile))
        menu.addAction(make_action(menu, t, "box_search", tr("Найти сборки в лаунчерах"), self._discover_now))
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
        menu.addAction(make_action(menu, t, "pencil", tr("Настройки сборки…"), lambda: self._edit_profile(name)))
        menu.addAction(make_action(menu, t, "folder", tr("Открыть папку"),
                                   lambda: self._open_path(self.profiles[name].get("mods_path", ""))))
        menu.addAction(make_action(menu, t, "layers", tr("Группа…"), lambda: self._set_profile_group(name)))
        if self.profiles[name].get("ignored_deps"):
            menu.addAction(make_action(menu, t, "undo", tr("Вернуть скрытые зависимости ({0})",
                                                          len(self.profiles[name]["ignored_deps"])),
                                       lambda: self._restore_ignored_dependencies(name)))
        if modpack_install.load_state(self.profiles[name].get("mods_path", "")):
            menu.addAction(make_action(menu, t, "download", tr("Обновить сборку из каталога…"),
                                       lambda: self._update_modpack(name)))
        menu.addSeparator()
        menu.addAction(make_action(menu, t, "copy", tr("Клонировать сборку…"), lambda: self._clone_profile(name)))
        others = [n for n in self.profiles if n != name]
        if others:
            compare = menu.addMenu(icons.icon("git_compare", t["text_muted"], 16), tr("Сравнить с…"))
            for other in others:
                compare.addAction(other, lambda o=other: self._compare_profiles(name, o))
        menu.addSeparator()
        menu.addAction(make_action(menu, t, "x", tr("Убрать из списка"), lambda: self._remove_profile(name), "danger"))
        menu.exec(pos)

    def _remove_profile(self, name: str):
        if len(self.profiles) <= 1:
            self.toasts.show(tr("Нельзя убрать единственную сборку."), "warning")
            return
        if not confirm(self, tr("Убрать сборку"), tr('Убрать «{0}» из списка?\nФайлы игры и моды не будут удалены.', name),
                       tr("Убрать"), tr("Отмена")):
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
        self.toasts.show(tr('Найдено новых сборок: {0}', added) if added else tr("Новых сборок не найдено."),
                         "success" if added else "info")

    def _on_settings_changed(self, settings: dict):
        view_keys = ("view_mode", "compact_rows", "expand_dependencies", "group_by_status", "columns", "abandoned_warning",
                     "update_min_age_days")
        view_changed = any(self.settings.get(k) != settings.get(k) for k in view_keys if k in settings)
        accent_changed = settings.get("accent") != self.settings.get("accent") or \
            settings.get("sidebar_image") != self.settings.get("sidebar_image")
        scale_changed = settings.get("ui_scale") != self.settings.get("ui_scale")
        language_changed = "language" in settings and \
            settings["language"] != (self.settings.get("language") or i18n.language())
        minimal_changed = settings.get("minimal_mode") != self.settings.get("minimal_mode")
        shortcuts_changed = settings.get("shortcuts") != self.settings.get("shortcuts")
        image = settings.get("sidebar_image")
        if image and os.path.isfile(image) and os.path.dirname(os.path.abspath(image)) != os.path.abspath(app_config.CONFIG_DIR):
            # Копия в папке программы: картинка не пропадёт, если исходный файл переместят
            target = os.path.join(app_config.CONFIG_DIR, "sidebar_background" + os.path.splitext(image)[1].lower())
            try:
                shutil.copyfile(image, target)
                settings["sidebar_image"] = target
            except OSError as e:
                logging.warning(tr('Не удалось скопировать картинку боковой панели: {0}', e))
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
            self.toasts.show(tr('Масштаб {0}% применится после перезапуска.', settings.get('ui_scale')), "info",
                             tr("Перезапустить"), self._restart_app, timeout=10000)
        if language_changed:
            # На обоих языках: пользователь мог выбрать язык, которого не читает текущий интерфейс
            self.toasts.show("Язык сменится после перезапуска · Language changes after a restart", "info",
                             "Перезапустить / Restart", self._restart_app, timeout=15000)

    def _clear_cache(self):
        if self._busy:
            self.toasts.show(tr("Дождитесь окончания текущей операции."), "warning")
            return
        for path in (app_config.CACHE_FILE,):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError as e:
                logging.error(tr('Не удалось удалить {0}: {1}', path, e))
        shutil.rmtree(app_config.ICON_CACHE_DIR, ignore_errors=True)
        os.makedirs(app_config.ICON_CACHE_DIR, exist_ok=True)
        self._icon_pixmaps.clear()
        self.icon_loader._pending.clear()
        self.sessions.clear()
        self.toasts.show(tr("Кэш очищен."), "success")

    # ================================================================== Сборки: порядок, группы, клон, сравнение

    def _profile_groups(self) -> List[str]:
        return sorted({p.get("group") or "" for p in self.profiles.values()} - {""})

    def _on_profiles_reordered(self, order: List[tuple]):
        if len(order) != len(self.profiles):
            return
        self.profiles = {name: {**self.profiles[name], "group": group} for name, group in order if name in self.profiles}
        self._refresh_sidebar()
        self.save_config()

    def _set_profile_group(self, name: str):
        groups = self._profile_groups()
        hint = tr("Пусто — без группы") + (tr('\nУже есть: {0}', ', '.join(groups)) if groups else "")
        group = ask_text(self, tr("Группа сборки"), hint, tr("Например: Серверы"), self.profiles[name].get("group") or "")
        if group is None and not self.profiles[name].get("group"):
            return
        self.profiles[name]["group"] = group or ""
        self._refresh_sidebar()
        self.save_config()

    def _clone_profile(self, name: str):
        profile = self.profiles.get(name)
        if not profile or not profile.get("mods_path") or self._busy:
            return
        dialog = CloneDialog(self, name, profile["mods_path"], self.profiles.keys())
        if not dialog.exec():
            return
        new_name, target, with_saves = dialog.values()

        def done(copied):
            versions = {"game_version": self.game_version, "loader": self.loader} if name == self.current_profile_name else {}
            self.profiles[new_name] = {**profile, **versions, "mods_path": target, "source": "manual",
                                       "manual_mode": True, "pinned": dict(profile.get("pinned") or {})}
            self._refresh_sidebar()
            self.save_config()
            self.toasts.show(tr('Сборка «{0}» создана: скопировано {1}.', new_name, ', '.join(copied)), "success",
                             tr("Сравнить"), lambda: self._compare_profiles(name, new_name), timeout=9000)

        self._run_task(clone_game_dir, (profile["mods_path"], target, with_saves), done,
                       lambda e: show_message(self, QMessageBox.Icon.Critical, tr("Не удалось клонировать"), e),
                       tr("Копируем сборку…"))

    def _profile_entries_task(self, name: str) -> List[dict]:
        """Записи модов сборки: из памяти или быстрым локальным сканированием (из потока)."""
        data = self.sessions.get(self._skey(name, MODS))
        if data is None:
            data = scan_local(MODS.path(self.profiles[name].get("mods_path", "")), app_config.CACHE_FILE, MODS)
        return [e for e in iter_all_entries(data) if e.get("filepath")]

    def _compare_profiles(self, left: str, right: str):
        if left not in self.profiles or right not in self.profiles:
            return

        def collect():
            return compare_entries(self._profile_entries_task(left), self._profile_entries_task(right))

        self._run_task(collect, (), lambda diff: CompareDialog(self, self.tokens, left, right, diff).exec(),
                       lambda e: show_message(self, QMessageBox.Icon.Warning, tr("Сравнение"), e), tr("Сравниваем сборки…"))
