"""Работа в фоне: значок в трее, фоновая проверка обновлений модов во всех сборках, уведомления Windows,
закрытие окна в трей и автозапуск с Windows.

Примесь к главному окну (ModUpdaterApp): методы работают с его атрибутами."""
import copy
import logging
import time
from typing import List, Optional

from PyQt6.QtCore import QEvent, QTimer
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

import background_check
from i18n import tr
from ui.tray import TrayIcon
from workers import BackgroundCheckWorker

TICK_MS = 5 * 60 * 1000  # Раз в 5 минут смотрим, не пора ли проверить
FIRST_CHECK_MS = 3 * 60 * 1000  # Первая фоновая проверка — через 3 минуты после запуска, когда всё загрузилось


class TrayMixin:
    # ================================================================== Значок в трее

    def _setup_tray(self):
        self._quitting = False
        self._bg_worker: Optional[BackgroundCheckWorker] = None
        self._bg_results: List[background_check.ProfileUpdates] = []
        self.tray: Optional[TrayIcon] = None
        if TrayIcon.available():
            self.tray = TrayIcon(self)
            self.tray.open_requested.connect(self._show_from_tray)
            self.tray.toggle_requested.connect(self._toggle_from_tray)
            self.tray.check_requested.connect(lambda: self._start_background_check(manual=True))
            self.tray.quit_requested.connect(self._quit_from_tray)
            self.tray.messageClicked.connect(self._on_tray_message_clicked)
            self._refresh_tray()
        self._bg_timer = QTimer(self, interval=TICK_MS)
        self._bg_timer.timeout.connect(self._background_tick)
        self._bg_timer.start()
        QTimer.singleShot(FIRST_CHECK_MS, self._background_tick)

    def _tray_wanted(self) -> bool:
        return bool(self.settings.get("close_to_tray") or getattr(self, "_started_in_tray", False))

    def _refresh_tray(self):
        if self.tray:
            self.tray.setVisible(self._tray_wanted() or not self.isVisible())

    def start_in_tray(self) -> bool:
        """Запуск с --tray (автозапуск с Windows): окно не показываем. False — трея нет, окно надо показать."""
        if not self.tray:
            return False
        self._started_in_tray = True
        self._refresh_tray()
        return True

    def _show_from_tray(self):
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            self._deactivated_at = time.monotonic()  # Щелчок по значку в трее сначала забирает у окна фокус
        super().changeEvent(event)

    def _toggle_from_tray(self):
        """Щелчок по значку: окно впереди — убрать (в трей или свернуть), иначе — показать."""
        in_front = self.isVisible() and not self.isMinimized() and (
            self.isActiveWindow() or time.monotonic() - getattr(self, "_deactivated_at", 0.0) < 1.0)
        if not in_front:
            self._show_from_tray()
        elif not self._hide_to_tray():
            self.showMinimized()

    def _hide_to_tray(self) -> bool:
        """Закрытие окна при включённом «оставаться в трее». False — трея нет, закрываемся по-настоящему."""
        if not self.tray or self._quitting or not self._tray_wanted():
            return False
        self.window_cfg["geometry"] = bytes(self.saveGeometry().toBase64()).decode()
        self.save_config()
        self.hide()
        self.tray.show()
        if not self.settings.get("tray_hint_shown"):
            self.settings["tray_hint_shown"] = True
            self.save_config()
            self.tray.showMessage("Minecraft Mod Updater",
                                  tr("Программа работает в трее и проверяет обновления модов. Выход — в меню значка."),
                                  QSystemTrayIcon.MessageIcon.Information, 6000)
        return True

    def _quit_from_tray(self):
        self._quitting = True
        if not self.isVisible():
            self.show()  # closeEvent спросит про незаконченное обновление в окне, а не в пустоте
        self.close()

    # ================================================================== Фоновая проверка

    def _background_due(self) -> bool:
        if not self.settings.get("background_check", True):
            return False
        hours = float(self.settings.get("background_interval_hours", 6) or 6)
        return time.time() - float(self.settings.get("background_last_check") or 0) >= hours * 3600

    def _background_tick(self):
        if self._background_due():
            self._start_background_check()

    def _start_background_check(self, manual: bool = False):
        if self._bg_worker and self._bg_worker.isRunning():
            return
        if self._busy or getattr(self, "_game_running", False):
            if manual:
                self.toasts.show(tr("Дождитесь окончания текущей операции."), "warning")
            return  # Не мешаем обновлению и игре — проверим на следующем такте
        config = {"profiles": copy.deepcopy(self.profiles), "settings": dict(self.settings)}
        self._bg_worker = BackgroundCheckWorker(config)
        self._bg_worker.finished_with.connect(lambda results: self._on_background_checked(results, manual))
        if self.tray:
            self.tray.set_checking(True)
        logging.info(tr("Фоновая проверка обновлений: сборок {0}", len(config["profiles"])))
        self._bg_worker.start()

    def _on_background_checked(self, results: List[background_check.ProfileUpdates], manual: bool):
        if self.tray:
            self.tray.set_checking(False)
        self.settings["background_last_check"] = time.time()
        self._bg_results = results
        with_updates = [r for r in results if r.updates]
        total = sum(len(r.updates) for r in with_updates)
        status = tr("Обновлений: {0} — {1}", total, background_check.summary(with_updates)) if total else \
            tr("Все моды актуальны")
        if self.tray:
            self.tray.set_status(status)
        notified = self.settings.get("notified_updates") or {}
        fresh = background_check.new_updates(with_updates, notified)
        # Запоминаем только текущие обновления: если мод обновили, а потом вышла ещё версия — сообщим снова
        self.settings["notified_updates"] = {r.name: r.keys() for r in with_updates}
        self.save_config()
        if fresh or (manual and total):
            self._announce_updates(fresh or with_updates)
        elif manual:
            self.toasts.show(tr("Все моды во всех сборках актуальны."), "success")

    def _announce_updates(self, results: List[background_check.ProfileUpdates]):
        total = sum(len(r.updates) for r in results)
        title = tr("Доступны обновления модов: {0}", total)
        text = background_check.summary(results)
        self._announced_profile = results[0].name
        if self.isVisible() and not self.isMinimized() and self.isActiveWindow():
            self.toasts.show(f"{title} ({text})", "info", tr("Показать"), self._on_tray_message_clicked, timeout=12000)
        elif self.tray and self.tray.isVisible():
            self._tray_click_action = None
            self.tray.showMessage(title, text + "\n" + tr("Нажмите, чтобы открыть."),
                                  QSystemTrayIcon.MessageIcon.Information, 10000)
        else:
            QApplication.alert(self)  # Без трея — мигаем кнопкой на панели задач

    def _on_tray_message_clicked(self):
        self._show_from_tray()
        action, self._tray_click_action = getattr(self, "_tray_click_action", None), None
        if action:  # Уведомление о загрузке: своё действие (открыть сборку и т.п.)
            action()
            return
        name = getattr(self, "_announced_profile", None)
        if name and name in self.profiles and name != self.current_profile_name:
            self._on_profile_selected(name)
        self._switch_page(self.PAGE_MODS_INDEX)

    def _stop_background_check(self):
        if self._bg_worker and self._bg_worker.isRunning():
            self._bg_worker.stop()

    # ================================================================== Автозапуск с Windows

    def _set_autostart(self, enabled: bool):
        import installer
        state = installer.state()
        exe = state.exe if state.installed else self._current_exe()
        try:
            installer.set_autostart(enabled, exe)
        except OSError as e:
            self.toasts.show(tr("Не удалось изменить автозапуск: {0}", e), "error")
            return
        if enabled and not self.settings.get("close_to_tray"):
            # Автозапуск в трей без «оставаться в трее» бессмыслен: окно закроют — фоновая проверка остановится
            self.settings["close_to_tray"] = True
            self.save_config()
        self.toasts.show(tr("Программа будет запускаться вместе с Windows (в трее).") if enabled
                         else tr("Автозапуск выключен."), "success")
        self._refresh_background_ui()

    @staticmethod
    def _current_exe() -> str:
        import sys
        return sys.executable

    def _refresh_background_ui(self):
        import installer
        supported = installer.supported()
        command = installer.autostart_command() if supported else None
        self.settings_page.set_background_state(autostart=bool(command), autostart_supported=supported,
                                                tray_available=self.tray is not None,
                                                last_check=self.settings.get("background_last_check"),
                                                updates=sum(len(r.updates) for r in self._bg_results))
