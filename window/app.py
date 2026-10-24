"""Программа: установка, внешние ссылки, обновление самой программы, перезапуск.

Примесь к главному окну (ModUpdaterApp): методы работают с его атрибутами."""
import os
import sys
import time
from typing import List

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

import config as app_config
import single_instance
from i18n import tr
from ui_components import confirm, show_message


class AppMixin:
    # ================================================================== Перезапуск (масштаб интерфейса)

    def _restart_app(self):
        if self._busy:
            self.toasts.show(tr("Дождитесь окончания текущей операции."), "warning")
            return
        self.save_config()
        try:
            single_instance.launch_new_instance(single_instance.external_args(sys.argv))
        except OSError as e:
            show_message(self, QMessageBox.Icon.Critical, tr("Не удалось перезапустить"), str(e))
            return
        QApplication.instance().closeAllWindows()

    # ================================================================== Обновление программы

    def _refresh_app_update_ui(self, checking: bool = False):
        release = getattr(self, "_app_release", None)
        current = app_config.APP_VERSION
        if checking:
            text = tr("Версия {0} · проверяем…", current)
        elif release:
            text = tr("Версия {0} · <span style='color:{1}'><b>доступна {2}</b></span>", current, self.tokens["accent"],
                      release.version)
        elif self.settings.get("app_update_last_check"):
            text = tr("Версия {0} · это последняя версия", current)
        else:
            text = tr("Версия {0}", current)
        self.settings_page.set_app_update_state(text, available=bool(release) and not checking, checking=checking)

    def _check_app_update(self, manual: bool = False):
        """Новая версия программы на GitHub. Автоматически — при запуске, не чаще раза в 12 часов."""
        import self_update
        if not manual:
            known = self_update.parse_version(self.settings.get("app_update_latest") or "")
            current = self_update.parse_version(app_config.APP_VERSION)
            newer_known = bool(known and current and known > current)  # Уже находили — напомним без ожидания
            if not self.settings.get("app_update_check", True) or \
                    not (newer_known or self_update.needs_check(self.settings.get("app_update_last_check"))):
                return
        self._refresh_app_update_ui(checking=True)
        self._run_task(self_update.check, (), lambda release: self._on_app_update(release, manual),
                       lambda error: self._on_app_update_failed(error, manual), None)

    def _on_app_update(self, release, manual: bool):
        self.settings["app_update_last_check"] = time.time()
        self.settings["app_update_latest"] = release.version if release else ""
        self.save_config()
        self._app_release = release
        self._refresh_app_update_ui()
        if release is None:
            if manual:
                self.toasts.show(tr("У вас последняя версия программы."), "success")
            return
        if manual:
            self._open_app_update()
        elif self.settings.get("app_update_skip") != release.version:
            self.toasts.show(tr("Доступна новая версия программы: {0}", release.version), "info", tr("Подробнее"),
                             self._open_app_update, timeout=15000)

    def _on_app_update_failed(self, error: str, manual: bool):
        self._refresh_app_update_ui()
        if manual:
            self.toasts.show(tr("Не удалось проверить обновления программы: {0}", error), "warning")

    def _open_app_update(self):
        import self_update
        from ui.dialogs import AppUpdateDialog
        release = getattr(self, "_app_release", None)
        if release is None:
            return
        if self._busy:
            self.toasts.show(tr("Дождитесь окончания текущей операции."), "warning")
            return
        dialog = AppUpdateDialog(self, release, app_config.APP_VERSION, self_update.supported())
        accepted = dialog.exec()
        if dialog.skipped:
            self.settings["app_update_skip"] = release.version
            self.save_config()
            self.toasts.show(tr("Версия {0} пропущена — напомним о следующей.", release.version), "info")
        elif accepted and dialog.downloaded_path:
            self._install_app_update(dialog.downloaded_path)

    def _install_app_update(self, path: str):
        """Ставит скачанный .exe вместо текущего и перезапускает программу."""
        import self_update
        if self._busy:
            self.toasts.show(tr("Дождитесь окончания текущей операции и обновите программу снова."), "warning")
            return
        try:
            self_update.replace_executable(path, self_update.current_executable())
        except OSError as e:
            show_message(self, QMessageBox.Icon.Critical, tr("Не удалось обновить программу"), str(e))
            return
        self._restart_app()

    # ================================================================== Установка и внешние ссылки

    def _refresh_install_ui(self):
        import installer
        supported = installer.supported()
        self.settings_page.set_install_state(supported, installer.state() if supported else None)

    def _offer_install(self):
        """Один раз предлагаем установить программу, если она запущена просто как .exe."""
        import installer
        if not installer.supported() or self.settings.get("install_offered") or installer.state().installed:
            return
        self.settings["install_offered"] = True
        self.save_config()
        self.toasts.show(tr("Установите программу: ярлыки, открытие .mrpack двойным щелчком и ссылки modupdater://."),
                         "info", tr("Подробнее"), lambda: self._switch_page(self.PAGE_SETTINGS_INDEX), timeout=12000)

    def _maybe_onboarding(self):
        """Мастер первого запуска: язык и тема → сборки → установка. Только если настроек ещё не было."""
        import i18n
        import installer
        from ui.onboarding import OnboardingDialog
        from ui.widgets import profile_subtitle
        if not getattr(self, "_first_run", False) or self.settings.get("onboarding_done"):
            return
        self.settings["onboarding_done"] = True
        self.settings["install_offered"] = True  # Установку предлагает сам мастер — без отдельного уведомления
        self.save_config()
        found = [{"name": name, "subtitle": profile_subtitle(p)} for name, p in self.profiles.items() if p.get("mods_path")]
        supported = installer.supported()
        dialog = OnboardingDialog(self, self.config_theme, found, supported, supported and installer.state().installed)
        if not dialog.exec():
            return
        self.save_config()  # Тема выбрана в мастере
        if dialog.language != i18n.language():
            self.settings["language"] = dialog.language
            self.save_config()
            self.toasts.show("Язык сменится после перезапуска · Language changes after a restart", "info",
                             "Перезапустить / Restart", self._restart_app, timeout=20000)
        if dialog.add_manually:
            QTimer.singleShot(0, self._add_profile)
        if dialog.install_options:
            QTimer.singleShot(200, lambda: self._install_app(dialog.install_options))

    def _install_app(self, options: dict):
        import installer
        try:
            done = installer.install(sys.executable, **options)
        except Exception as e:
            show_message(self, QMessageBox.Icon.Critical, tr("Установка не удалась"), str(e))
            return
        self._refresh_install_ui()
        state = installer.state()
        text = tr("Готово:\n") + "\n".join(f"• {step}" for step in done)
        if not state.running_installed:
            if confirm(self, tr("Программа установлена"), text + tr("\n\nЗапустить установленную копию и закрыть эту?"),
                       tr("Запустить"), tr("Позже")):
                self.save_config()
                try:
                    single_instance.launch_new_instance(executable=state.exe)
                except OSError as e:
                    show_message(self, QMessageBox.Icon.Critical, tr("Не удалось запустить"), str(e))
                    return
                QApplication.instance().closeAllWindows()
        else:
            show_message(self, QMessageBox.Icon.Information, tr("Настройки установки применены"), text)

    def _uninstall_app(self):
        import installer
        if not confirm(self, tr("Удалить программу"), tr("Удалить ярлыки, связь с .mrpack и ссылками и саму программу? "
                       "Сборки, моды и бэкапы не затрагиваются, настройки сохранятся."), tr("Удалить"), tr("Отмена")):
            return
        installer.uninstall(schedule_exe_removal=installer.state().running_installed)
        show_message(self, QMessageBox.Icon.Information, tr("Программа удалена"),
                     tr("Ярлыки и записи удалены. Окно закроется — папка программы будет удалена следом."))
        QApplication.instance().closeAllWindows()

    def handle_external(self, args: List[str]):
        """Файл .mrpack или ссылка modupdater://, переданные при запуске или второй копией программы."""
        import urllib.parse
        if list(args or []) == ["--tray"]:  # Повторный автозапуск — программа и так работает
            return
        self._show_from_tray()
        QApplication.alert(self)
        for arg in args or []:
            if arg == "--activate":
                continue
            if arg.lower().endswith(".mrpack") and os.path.isfile(arg):
                self._switch_page(self.PAGE_MODS_INDEX)
                self._import_mrpack(arg)
                continue
            if not arg.lower().startswith("modupdater:"):
                continue
            parsed = urllib.parse.urlparse(arg)
            action = (parsed.netloc or parsed.path.strip("/").split("/")[0]).lower()
            rest = parsed.path.strip("/") if parsed.netloc else "/".join(parsed.path.strip("/").split("/")[1:])
            value = urllib.parse.unquote(rest)
            query = urllib.parse.parse_qs(parsed.query)
            if action == "import" and value:
                self._switch_page(self.PAGE_MODS_INDEX)
                self._import_text(value)
            elif action == "mod" and value:  # modupdater://mod/sodium — найти в каталоге
                self._switch_page(self.PAGE_CATALOG_INDEX)
                self.catalog_page.search.setText(value)
            elif action == "profile" and value in self.profiles:
                self._switch_page(self.PAGE_MODS_INDEX)
                self._on_profile_selected(value)
                self._refresh_sidebar()
            elif action == "mrpack" and query.get("url"):
                self._open_remote_mrpack(query["url"][0])
            else:
                self.toasts.show(tr('Неизвестная ссылка: {0}', arg), "warning")

    def _open_remote_mrpack(self, url: str):
        if not url.lower().startswith("https://"):
            self.toasts.show(tr("Сборку можно открыть только по адресу https://"), "warning")
            return
        import tempfile
        from mod_updater_logic import download_file
        target = os.path.join(tempfile.gettempdir(), "modupdater-import.mrpack")
        self._run_task(download_file, (url, target), lambda _: self._import_mrpack(target),
                       lambda e: self.toasts.show(tr('Не удалось скачать сборку: {0}', e), "error"), tr("Скачиваем сборку…"))
