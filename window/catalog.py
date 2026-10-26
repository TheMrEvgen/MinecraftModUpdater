"""Каталог Modrinth: установка, массовая установка, выбранная версия, статистика и альтернативы.

Примесь к главному окну (ModUpdaterApp): методы работают с его атрибутами."""
import os
from typing import Dict, List, Optional

from PyQt6.QtWidgets import QMessageBox

import config as app_config
from content import CONTENT_TYPES, ContentType, MODS
from enums import ModStatus
from i18n import tr
from scanner import resolve_many_installs, resolve_project_install, scan_local
from ui_components import show_message
from view_model import iter_all_entries
from window.common import PAGE_DOWNLOADS


class CatalogMixin:
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
        for content in CONTENT_TYPES:
            key = self._skey(content=content)
            folder = self._content_folder(content)
            if content is not self.content and key not in self.sessions and folder and os.path.isdir(folder):
                self._run_task(scan_local, (folder, app_config.CACHE_FILE, content),
                               lambda data, k=key: self._on_catalog_folder_read(k, data), lambda e: None, None)

    def _on_catalog_folder_read(self, key: tuple, data: List[dict]):
        self.sessions.setdefault(key, data)
        self.catalog_page.set_context(self.game_version, self.loader, self._installed_ids())

    def _install_from_catalog(self, hit: dict):
        if self.catalog_page.content.key == "modpacks":
            self._install_modpack(hit)
            return
        if self._busy:
            self.toasts.show(tr("Дождитесь окончания текущей операции."), "warning")
            return
        content = self.catalog_page.content
        if not self._profile().get("mods_path"):
            self.toasts.show(tr("Сначала укажите папку игры для этой сборки."), "warning", tr("Настроить"), self._edit_current_profile)
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
        self.toasts.show(tr('Не удалось подготовить установку: {0}', error), "error")

    def _on_catalog_resolved(self, hit: dict, content: ContentType, entries: List[dict]):
        pid = hit.get("project_id")
        self.catalog_page.set_installing([pid], False)
        if not entries:
            self.toasts.show(tr('У «{0}» нет версии для Minecraft {1}', hit.get('title'), self.game_version)
                             + (f" ({self.loader})" if content is MODS else "") + ".", "warning")
            return
        manual = [e for e in entries if not e.get("download_url") and e.get("manual_url")]
        if manual:
            self._offer_manual_downloads(manual)
            entries = [e for e in entries if e.get("download_url")]
            if not entries:
                return
        if len(entries) > 1:
            deps = ", ".join(e.get("name") for e in entries[1:])
            self.toasts.show(tr('Вместе с «{0}» будут установлены зависимости: {1}', entries[0].get('name'), deps), "info")
        folder = self._content_folder(content)
        if not folder:  # Датапак без мира: ставить некуда
            self.catalog_page.set_installing([e["project_id"] for e in entries], False)
            self.toasts.show(tr("Датапаки ставятся в мир, а миров в этой сборке пока нет — создайте мир в игре."), "warning")
            return
        self._catalog_installing = [e["project_id"] for e in entries]
        self._catalog_content = content
        self.catalog_page.set_installing(self._catalog_installing, True)
        self.start_update(entries, folder=folder)
        if not self.update_worker:  # Установка не началась (игра запущена или отмена)
            self.catalog_page.set_installing(self._catalog_installing, False)
            self._catalog_installing = []

    def _offer_manual_downloads(self, entries: List[dict]):
        """Автор разрешает скачивать мод только с сайта CurseForge — открываем страницу файла в браузере."""
        import webbrowser
        names = ", ".join(e.get("name") or "?" for e in entries)
        self.toasts.show(tr("{0}: автор разрешает скачивать только с сайта CurseForge. Скачайте файл и перетащите его "
                            "в окно программы.", names), "info", tr("Открыть страницу"),
                         lambda: [webbrowser.open(e["manual_url"]) for e in entries], timeout=20000)

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
            names = ", ".join(e.get("name") for e in succeeded[:3]) + (tr(" и др.") if len(succeeded) > 3 else "")
            self.toasts.show(tr('Установлено: {0}', names), "success", tr("Отменить"),
                             lambda: self._rollback_dir(worker.transaction.dir), timeout=9000)
        if failed:
            self.toasts.show(tr('Не удалось установить: {0}', ', '.join(e.get('name') for e in failed)), "error")
        if os.path.normcase(worker.mods_path) == os.path.normcase(self.mods_path) and not self.is_import_mode:
            self.start_local_scan()

    # ================================================================== Статистика и замена заброшенных модов

    def _refresh_stats(self):
        self.stats_page.set_data(self._count_items, self.current_profile_name, self.mod_tags)
        self.stats_page.set_launches(self._profile().get("launches"))

    def _stats_filter(self, query: str):
        self._switch_page(self.PAGE_MODS_INDEX)
        self._set_category_all_silent()
        self.search.setText(query)
        self._apply_filter(query=query)

    def _set_category_all_silent(self):
        from view_model import CAT_ALL
        self.cards[CAT_ALL].setChecked(True)
        self.proxy.set_filter(category=CAT_ALL)

    def _find_replacement(self, entry: dict):
        self._switch_page(self.PAGE_CATALOG_INDEX)
        self.catalog_page.tab_group.buttons()[CONTENT_TYPES.index(self.content)].setChecked(True)
        self.catalog_page.content = self.content
        self.catalog_page.search_like(entry)

    # ================================================================== Каталог: массовая установка и выбранная версия

    def _install_many_from_catalog(self, hits: List[dict]):
        if self.catalog_page.content.key == "modpacks":
            self.toasts.show(tr("Сборки ставятся по одной — нажмите «Установить» у нужной."), "info")
            return
        if self._busy or not hits:
            if self._busy:
                self.toasts.show(tr("Дождитесь окончания текущей операции."), "warning")
            return
        content = self.catalog_page.content
        installed = self._installed_ids().get(content.key, set())
        ids = [h.get("project_id") for h in hits]
        self.catalog_page.set_installing(ids, True)

        def resolved(result):
            entries, missing = result
            self.catalog_page.set_installing(ids, False)
            if missing:
                titles = [h.get("title") for h in hits if h.get("project_id") in missing]
                self.toasts.show(tr('Нет версии для вашей сборки: {0}', ', '.join(titles)), "warning")
            manual = [e for e in entries if not e.get("download_url") and e.get("manual_url")]
            if manual:
                self._offer_manual_downloads(manual)
                entries = [e for e in entries if e.get("download_url")]
            if entries:
                self._start_catalog_install(entries, content)

        self._run_task(resolve_many_installs, (ids, self.game_version, self.loader, self._channel(), installed, content),
                       resolved, lambda e: (self.catalog_page.set_installing(ids, False),
                                            self.toasts.show(tr('Не удалось подготовить установку: {0}', e), "error")), None)

    def _install_modpack(self, hit: dict):
        """Готовая сборка из каталога: новая сборка Prism, установка Minecraft Launcher или папка. Качается в фоне."""
        from launchers import _appdata, _prism_instance_dir
        from ui.modpack_dialog import ModpackInstallDialog
        prism_root = os.path.join(_appdata(), "PrismLauncher")
        prism = _prism_instance_dir(prism_root) if os.path.isdir(prism_root) else None
        default = os.path.join(os.path.expanduser("~"), "MinecraftModpacks")  # Латиница: Java не любит кириллицу в путях
        import official_launcher
        minecraft = official_launcher.minecraft_dir()
        dialog = ModpackInstallDialog(self, hit, prism, default, os.path.join(app_config.CONFIG_DIR, "modpacks"),
                                      minecraft if os.path.isdir(minecraft) else None)
        if not dialog.exec() or not dialog.target:
            return
        import downloads
        from ui.modpack_dialog import _Worker
        plan, target = dialog.plan, dialog.target
        title = target.name or os.path.basename(target.instance_dir or target.game_dir)
        worker = _Worker("install", hit.get("project_id") or "", plan=plan, target=target, title=hit.get("title") or "")
        job = worker.download = self._start_download(tr("Сборка «{0}»", title), "modpack", worker.stop)

        def failed(error: str):
            import shutil
            shutil.rmtree(target.instance_dir or target.game_dir, ignore_errors=True)  # Папка новая — убираем начатое
            job.finish(downloads.FAILED if error else downloads.CANCELLED, error or tr("Отменено"))
            if error:
                self.toasts.show(tr("Сборка «{0}» не установлена: {1}", title, error), "error", timeout=15000)

        worker.installed.connect(lambda errors: self._on_modpack_installed(plan, target, errors, job))
        worker.failed.connect(failed)
        worker.start()
        self.toasts.show(tr("«{0}» качается в фоне.", title), "info", tr("Загрузки"),
                         lambda: self._switch_page(PAGE_DOWNLOADS), timeout=8000)

    def _on_modpack_installed(self, plan, target, errors: list, job):
        import webbrowser

        import downloads
        job.finish(downloads.FAILED if errors else downloads.DONE,
                   tr("Не скачалось файлов: {0}", len(errors)) if errors else tr("Установлена"))
        name, n = target.name or os.path.basename(target.instance_dir or target.game_dir), 2
        base = name
        while name in self.profiles:
            name, n = f"{base} ({n})", n + 1
        self.profiles[name] = {**app_config.default_profile(), "mods_path": target.game_dir,
                               "game_version": plan.game_version or "", "loader": plan.loader or "fabric",
                               "loader_version": plan.loader_version or "", "manual_mode": True, "source": target.source}
        if target.launcher_id:
            self.profiles[name]["launcher_id"] = target.launcher_id
        self.save_config()
        self._refresh_sidebar()
        if not self._busy:  # Пользователь ничем не занят — сразу показываем новую сборку
            self._load_profile(name)
            self._refresh_sidebar()
        if target.instance_dir or target.launcher_id:
            self.toasts.show(tr("Сборка «{0}» установлена в {1}.", name,
                                "Prism Launcher" if target.instance_dir else "Minecraft Launcher"),
                             "success", tr("Открыть"), lambda: self._load_profile(name), timeout=15000)
        else:
            self.toasts.show(tr("Сборка «{0}» установлена в {1}. Поставьте загрузчик {2} {3} в лаунчере и укажите эту "
                                "папку как папку игры.", name, target.game_dir, plan.loader or "", plan.loader_version or ""),
                             "success", timeout=20000)
        if plan.manual:
            self.toasts.show(tr("Скачайте с сайта CurseForge (автор запретил программам): {0}",
                                ", ".join(f.name for f in plan.manual[:5])), "warning", tr("Открыть страницы"),
                             lambda: [webbrowser.open(f.manual_url) for f in plan.manual if f.manual_url], timeout=30000)
        if errors:
            show_message(self, QMessageBox.Icon.Warning, tr("Не всё скачалось"), "\n".join(errors[:12]))

    # --- Обновление готовой сборки до новой версии ---

    def _check_modpack_update(self):
        """При открытии сборки из каталога: не вышла ли её новая версия (раз за запуск программы)."""
        import modpack_install
        name, game_dir = self.current_profile_name, self._profile().get("mods_path", "")
        if name in self._modpack_checked or not self.settings.get("auto_check", True) \
                or not modpack_install.load_state(game_dir):
            return
        self._modpack_checked.add(name)

        def done(result):
            if result and name == self.current_profile_name:
                self.toasts.show(tr("Вышла новая версия сборки «{0}»: {1}", result["state"].get("title") or name,
                                    result["latest"].get("version")), "info", tr("Обновить"),
                                 lambda: self._update_modpack(name), timeout=15000)

        self._run_task(modpack_install.check_update, (game_dir,), done, lambda e: None, None)

    def _update_modpack(self, name: Optional[str] = None):
        import modpack_install
        from ui.modpack_dialog import ModpackUpdateDialog
        if self._busy:
            self.toasts.show(tr("Дождитесь окончания текущей операции."), "warning")
            return
        if name and name != self.current_profile_name:
            self._load_profile(name)
        profile = self._profile()
        game_dir = profile.get("mods_path", "")
        state = modpack_install.load_state(game_dir)
        if not state or not self._confirm_game_not_running([]):
            return
        dialog = ModpackUpdateDialog(self, state, game_dir, os.path.join(app_config.CONFIG_DIR, "modpacks"))
        if not dialog.exec() or not dialog.update:
            return
        import downloads
        from ui.modpack_dialog import _Worker
        plan = dialog.plan
        old = (profile.get("game_version") or self.game_version, profile.get("loader_version") or self.current_loader_version)
        title = state.get("title") or self.current_profile_name
        worker = _Worker("update", plan=plan, game_dir=game_dir, update=dialog.update)
        job = worker.download = self._start_download(tr("Обновление сборки «{0}»", title), "modpack", worker.stop)
        self._set_busy("task", tr("Обновляем сборку «{0}»…", title))  # Файлы сборки меняются — другие операции ждут

        def failed(error: str):
            self._set_busy(None)
            job.finish(downloads.FAILED if error else downloads.CANCELLED, tr("Сборка осталась как была"))
            if error:
                self.toasts.show(tr("Сборка «{0}» не обновлена: {1}", title, error) + " " + tr("Сборка осталась как была."),
                                 "error", timeout=15000)

        def updated(backup: str):
            self._set_busy(None)
            job.finish(downloads.DONE, tr("Обновлена до {0}", plan.version))
            self._after_modpack_update(game_dir, title, plan, backup, old)

        worker.installed.connect(updated)
        worker.failed.connect(failed)
        worker.start()

    def _after_modpack_update(self, game_dir: str, title: str, plan, backup: str, old: tuple):
        if self._profile().get("mods_path") == game_dir:
            self._forget_profile_lists()

        def finish(launcher_change):
            launcher, launcher_backup = launcher_change
            self._after_launcher_change(plan.game_version if plan.game_version != old[0] else None,
                                        plan.loader_version if plan.loader_version != old[1] else None)
            self.toasts.show(tr("Сборка «{0}» обновлена до {1}.", title, plan.version), "success", tr("Отменить"),
                             lambda: self._undo_modpack_update(game_dir, backup, launcher, launcher_backup, old),
                             timeout=20000)
            if plan.manual:
                import webbrowser
                self.toasts.show(tr("Скачайте с сайта CurseForge (автор запретил программам): {0}",
                                    ", ".join(f.name for f in plan.manual[:5])), "warning", tr("Открыть страницы"),
                                 lambda: [webbrowser.open(f.manual_url) for f in plan.manual if f.manual_url],
                                 timeout=30000)

        launcher = self._launcher_target()
        if not launcher or (plan.game_version, plan.loader_version) == old or not plan.game_version:
            finish((None, None))
            return
        import launcher_pack
        launcher.game_version = old[0]

        def work():
            return launcher, launcher_pack.change_versions(launcher, plan.game_version, plan.loader, plan.loader_version)

        self._run_task(work, (), finish, lambda e: (finish((None, None)), show_message(
            self, QMessageBox.Icon.Warning, tr("Не удалось сменить версию в лаунчере"),
            tr("{0}\n\nПоставьте Minecraft {1} и {2} {3} в лаунчере вручную.", e, plan.game_version, plan.loader or "",
               plan.loader_version or ""))), None)

    def _forget_profile_lists(self):
        """Сборка изменилась целиком: забываем списки всех типов контента и перечитываем текущий."""
        for content in CONTENT_TYPES:
            self.sessions.pop(self._skey(content=content), None)
        self.start_local_scan()

    def _undo_modpack_update(self, game_dir: str, backup: str, launcher, launcher_backup: Optional[str], old: tuple):
        import launcher_pack
        import modpack_install
        if not self._confirm_game_not_running([]):
            return
        try:
            modpack_install.undo_update(backup, game_dir)
            if launcher and launcher_backup:
                launcher_pack.undo(launcher, launcher_backup)
        except (OSError, ValueError) as e:
            show_message(self, QMessageBox.Icon.Critical, tr("Не удалось вернуть"), str(e))
            return
        if self._profile().get("mods_path") == game_dir:
            self._after_launcher_change(*old)
            self._forget_profile_lists()
        self.toasts.show(tr("Сборка возвращена к прежней версии."), "info")

    def _install_catalog_version(self, hit: dict, version: dict):
        if self._busy:
            self.toasts.show(tr("Дождитесь окончания текущей операции."), "warning")
            return
        content = self.catalog_page.content
        installed = self._installed_ids().get(content.key, set())
        pid = hit.get("project_id")
        self.catalog_page.set_installing([pid], True)
        self._run_task(resolve_project_install,
                       (pid, self.game_version, self.loader, self._channel(), installed, content, 3, version),
                       lambda entries: self._on_catalog_resolved(hit, content, entries),
                       lambda e: self._on_catalog_failed(pid, e), None)

    def _start_catalog_install(self, entries: List[dict], content):
        folder = self._content_folder(content)
        if not folder:  # Датапак без мира: ставить некуда
            self.catalog_page.set_installing([e["project_id"] for e in entries], False)
            self.toasts.show(tr("Датапаки ставятся в мир, а миров в этой сборке пока нет — создайте мир в игре."), "warning")
            return
        self._catalog_installing = [e["project_id"] for e in entries]
        self._catalog_content = content
        self.catalog_page.set_installing(self._catalog_installing, True)
        self.start_update(entries, folder=folder)
        if not self.update_worker:
            self.catalog_page.set_installing(self._catalog_installing, False)
            self._catalog_installing = []

    def _installed_version(self, project_id: str) -> Optional[str]:
        """Установленная версия проекта (для отметки в списке версий каталога)."""
        content = self.catalog_page.content
        data = self.mods_data if content is self.content else self.sessions.get(self._skey(content=content)) or []
        return next((e.get("current_version") for e in iter_all_entries(data)
                     if e.get("project_id") == project_id and e.get("filepath")), None)

    def _show_installed_from_catalog(self, hit: dict):
        content = self.catalog_page.content
        self._switch_page(self.PAGE_MODS_INDEX)
        if self.content is not content:
            self._set_content(content)
        entry = next((e for e in iter_all_entries(self.mods_data) if e.get("project_id") == hit.get("project_id")
                      and e.get("filepath")), None)
        if entry:
            self._reset_filters()
            self._select_entry(entry)
            self._open_current_details()
        else:
            self.toasts.show(tr("Мод установлен — список обновится после чтения папки."), "info")
