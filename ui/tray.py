"""Значок в области уведомлений Windows: меню, подсказка с числом обновлений, уведомления."""
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from i18n import tr


class TrayIcon(QSystemTrayIcon):
    open_requested = pyqtSignal()
    toggle_requested = pyqtSignal()  # Щелчок по значку: открыть окно или, если оно уже впереди, убрать
    check_requested = pyqtSignal()
    quit_requested = pyqtSignal()

    def __init__(self, parent=None):
        icon = QApplication.windowIcon()
        super().__init__(icon if not icon.isNull() else QIcon(), parent)
        self.menu = QMenu()
        self.open_action = QAction(tr("Открыть Mod Updater"), self.menu)
        self.open_action.triggered.connect(self.open_requested)
        self.check_action = QAction(tr("Проверить обновления модов сейчас"), self.menu)
        self.check_action.triggered.connect(self.check_requested)
        quit_action = QAction(tr("Выход"), self.menu)
        quit_action.triggered.connect(self.quit_requested)
        self.menu.addAction(self.open_action)
        self.menu.addAction(self.check_action)
        self.menu.addSeparator()
        self.menu.addAction(quit_action)
        self.setContextMenu(self.menu)
        self.activated.connect(self._on_activated)
        self.set_status("")

    def _on_activated(self, reason):
        # Щелчок — открыть или убрать окно, двойной щелчок — открыть; правая кнопка — меню (его показывает Qt)
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_requested.emit()
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_requested.emit()

    def set_status(self, text: str):
        self.setToolTip("Minecraft Mod Updater" + (f"\n{text}" if text else ""))

    def set_checking(self, checking: bool):
        self.check_action.setEnabled(not checking)
        self.check_action.setText(tr("Проверяем обновления…") if checking else tr("Проверить обновления модов сейчас"))

    @staticmethod
    def available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()
