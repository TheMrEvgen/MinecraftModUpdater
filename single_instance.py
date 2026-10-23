"""
Один экземпляр программы: повторный запуск (двойной щелчок по .mrpack, ссылка modupdater://) передаёт
аргументы уже открытому окну и завершается.
"""
import getpass
import json
import logging
import os
import subprocess
import sys
import time
from typing import List, Optional

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from i18n import tr

SERVER_NAME = f"MinecraftModUpdater-{getpass.getuser()}"
RESTART_FLAG = "--restarted"  # Новая копия при перезапуске: дождаться закрытия старой, а не передавать ей аргументы


def external_args(argv: List[str]) -> List[str]:
    """Файлы .mrpack и ссылки modupdater:// из командной строки."""
    return [a for a in argv[1:] if a.lower().endswith(".mrpack") or a.lower().startswith("modupdater:")]


def send_to_running(args: List[str], timeout_ms: int = 400) -> bool:
    """True, если программа уже запущена и получила аргументы."""
    socket = QLocalSocket()
    socket.connectToServer(SERVER_NAME)
    if not socket.waitForConnected(timeout_ms):
        return False
    socket.write(json.dumps(args).encode("utf-8"))
    socket.flush()
    socket.waitForBytesWritten(timeout_ms)
    socket.disconnectFromServer()
    if socket.state() != QLocalSocket.LocalSocketState.UnconnectedState:
        socket.waitForDisconnected(timeout_ms)
    return True


def is_running(timeout_ms: int = 200) -> bool:
    """Открыта ли другая копия. Пустое подключение сервер не считает командой."""
    socket = QLocalSocket()
    socket.connectToServer(SERVER_NAME)
    connected = socket.waitForConnected(timeout_ms)
    socket.abort()
    return connected


def wait_for_previous(timeout_s: float = 20) -> bool:
    """Ждёт, пока закроется прежняя копия (перезапуск, обновление). True — дождались."""
    deadline = time.monotonic() + timeout_s
    while is_running():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.2)
    return True


def launch_new_instance(args: List[str] = (), executable: Optional[str] = None) -> subprocess.Popen:
    """Запускает новую копию программы отдельным процессом (перезапуск текущей или установленный .exe)."""
    if executable:
        command = [executable]
    else:
        command = [sys.executable] + ([] if getattr(sys, "frozen", False) else [os.path.abspath(sys.argv[0])])
    command += [a for a in args if a != RESTART_FLAG] + [RESTART_FLAG]
    # Сборка PyInstaller: новая копия должна распаковаться заново, а не взять временную папку текущей
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if sys.platform == "win32" else 0
    return subprocess.Popen(command, env=env, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class InstanceServer(QObject):
    received = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.server = QLocalServer(self)
        QLocalServer.removeServer(SERVER_NAME)  # Остаток после аварийного завершения
        if not self.server.listen(SERVER_NAME):
            logging.warning(tr('Не удалось открыть канал одного экземпляра: {0}', self.server.errorString()))
        self.server.newConnection.connect(self._on_connection)

    def _on_connection(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            buffer = bytearray()
            # Данные могут прийти раньше подписки на readyRead — читаем сразу, дочитываем по сигналам
            socket.readyRead.connect(lambda s=socket, b=buffer: b.extend(bytes(s.readAll())))
            socket.disconnected.connect(lambda s=socket, b=buffer: self._finish(s, b))
            buffer.extend(bytes(socket.readAll()))
            if socket.state() != QLocalSocket.LocalSocketState.ConnectedState:
                self._finish(socket, buffer)

    def _finish(self, socket, buffer: bytearray):
        if getattr(socket, "_handled", False):
            return
        socket._handled = True
        buffer.extend(bytes(socket.readAll()))
        socket.deleteLater()
        if not buffer:  # Проверка «открыта ли программа» (is_running), а не команда
            return
        try:
            args = json.loads(bytes(buffer).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            args = []
        self.received.emit(args if isinstance(args, list) else [])
