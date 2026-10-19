"""
Один экземпляр программы: повторный запуск (двойной щелчок по .mrpack, ссылка modupdater://) передаёт
аргументы уже открытому окну и завершается.
"""
import getpass
import json
import logging
from typing import List

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

SERVER_NAME = f"MinecraftModUpdater-{getpass.getuser()}"


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


class InstanceServer(QObject):
    received = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.server = QLocalServer(self)
        QLocalServer.removeServer(SERVER_NAME)  # Остаток после аварийного завершения
        if not self.server.listen(SERVER_NAME):
            logging.warning(f"Не удалось открыть канал одного экземпляра: {self.server.errorString()}")
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
        try:
            args = json.loads(bytes(buffer).decode("utf-8")) if buffer else []
        except (ValueError, UnicodeDecodeError):
            args = []
        self.received.emit(args if isinstance(args, list) else [])
        socket.deleteLater()
