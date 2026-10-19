"""
Окно при сбое программы: необработанное исключение (в главном потоке или в фоновом) не роняет программу молча,
а показывает понятное окно с отчётом — его можно скопировать, сохранить или отправить на GitHub.
Отчёт также сохраняется в папке логов.
"""
import datetime
import logging
import os
import platform
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from typing import Optional

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QFontDatabase, QGuiApplication
from PyQt6.QtWidgets import QApplication, QFileDialog, QPlainTextEdit

import config as app_config

LOG_TAIL_LINES = 80


def build_report(exc_type, exc, tb) -> str:
    lines = [
        f"Minecraft Mod Updater {app_config.APP_VERSION}",
        f"Время: {datetime.datetime.now().isoformat(timespec='seconds')}",
        f"Система: {platform.platform()} · Python {platform.python_version()}",
        "",
        "".join(traceback.format_exception(exc_type, exc, tb)).rstrip(),
    ]
    log_path = app_config.CURRENT_LOG
    if log_path and os.path.isfile(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-LOG_TAIL_LINES:]
            lines += ["", f"--- Последние строки лога ({os.path.basename(log_path)}) ---", "".join(tail).rstrip()]
        except OSError:
            pass
    return "\n".join(lines)


def save_report(report: str) -> Optional[str]:
    try:
        os.makedirs(app_config.LOG_DIR, exist_ok=True)
        path = os.path.join(app_config.LOG_DIR, f"crash_{datetime.datetime.now():%Y-%m-%d_%H-%M-%S}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(report)
        return path
    except OSError:
        return None


def issue_url(exc_type, exc, report: str) -> Optional[str]:
    """Ссылка на новый issue с заполненным заголовком и отчётом (обрезается под ограничение длины адреса)."""
    if not app_config.GITHUB_REPOSITORY:
        return None
    title = f"Сбой: {exc_type.__name__}: {str(exc)[:80]}"
    body = f"**Что я делал перед сбоем:**\n\n\n**Отчёт:**\n```\n{report[:5500]}\n```"
    query = urllib.parse.urlencode({"title": title, "body": body})
    return f"https://github.com/{app_config.GITHUB_REPOSITORY}/issues/new?{query}"


class _Bridge(QObject):
    """Исключения из фоновых потоков показываем в главном потоке."""
    raised = pyqtSignal(object, object, object)


class CrashHandler:
    def __init__(self):
        self.bridge = _Bridge()
        self.bridge.raised.connect(self._show, Qt.ConnectionType.QueuedConnection)
        self._showing = False

    def install(self):
        sys.excepthook = self._hook
        threading.excepthook = lambda args: self._hook(args.exc_type, args.exc_value, args.exc_traceback)

    def _hook(self, exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logging.critical("Необработанное исключение", exc_info=(exc_type, exc, tb))
        if QApplication.instance() is None:
            sys.__excepthook__(exc_type, exc, tb)
            return
        self.bridge.raised.emit(exc_type, exc, tb)

    def _show(self, exc_type, exc, tb):
        if self._showing:  # Сбой внутри окна сбоя — только в лог
            return
        self._showing = True
        try:
            report = build_report(exc_type, exc, tb)
            path = save_report(report)
            CrashDialog(exc_type, exc, report, path).exec()
        except Exception:
            sys.__excepthook__(exc_type, exc, tb)
        finally:
            self._showing = False


def CrashDialog(exc_type, exc, report: str, saved_path: Optional[str]):
    from ui.dialogs import FramelessDialog
    from ui.widgets import label
    parent = QApplication.activeWindow()
    dialog = FramelessDialog(parent, "Программа столкнулась с ошибкой", 620)
    dialog.resize(720, 560)
    dialog.body.addWidget(label("Что-то пошло не так, но ваши моды в безопасности: все изменения папок "
                                "выполняются с бэкапом. Можно продолжить работу или перезапустить программу.",
                                None, wrap=True))
    error = label(f"{exc_type.__name__}: {exc}", "WarningText", wrap=True)
    error.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    dialog.body.addWidget(error)
    details = QPlainTextEdit(report)
    details.setReadOnly(True)
    mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    mono.setPointSizeF(8.5)
    details.setFont(mono)
    dialog.body.addWidget(details, 1)
    if saved_path:
        dialog.body.addWidget(label(f"Отчёт сохранён: {saved_path}", "Faint", wrap=True))

    def copy():
        QGuiApplication.clipboard().setText(report)
        copy_btn.setText("Скопировано ✓")

    def save_as():
        path, _ = QFileDialog.getSaveFileName(dialog, "Сохранить отчёт", "mod-updater-crash.txt", "Текст (*.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(report)

    def restart():
        from PyQt6.QtCore import QProcess
        args = sys.argv[1:] if getattr(sys, "frozen", False) else [os.path.abspath(sys.argv[0])] + sys.argv[1:]
        QProcess.startDetached(sys.executable, args)
        QApplication.instance().quit()

    copy_btn = dialog.add_button("Скопировать отчёт", False, copy)
    dialog.add_button("Сохранить…", False, save_as)
    url = issue_url(exc_type, exc, report)
    if url:
        dialog.add_button("Сообщить на GitHub", False, lambda: (copy(), webbrowser.open(url)))
    dialog.add_button("Перезапустить", False, restart)
    dialog.add_button("Продолжить работу", True, dialog.accept)
    dialog.finish_layout()
    return dialog


handler: Optional[CrashHandler] = None


def install():
    global handler
    handler = CrashHandler()
    handler.install()
