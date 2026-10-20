"""Окно быстрых команд (Ctrl+K): поиск по действиям, сборкам и модам с выполнением по Enter."""
from dataclasses import dataclass
from typing import Callable, List, Optional

from PyQt6.QtCore import QEvent, QRect, QRectF, QSize, Qt
from PyQt6.QtGui import QFont, QFontMetrics, QPainter
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QFrame, QLineEdit, QListWidget, QListWidgetItem, QStyle, QStyledItemDelegate, QVBoxLayout,
    QWidget,
)

import icons
from theme import qcolor
from ui.widgets import add_shadow
from i18n import tr

INDEX_ROLE = Qt.ItemDataRole.UserRole + 1
MAX_RESULTS = 60


@dataclass
class Command:
    title: str
    action: Callable[[], None]
    icon: str = "command"
    group: str = tr("Действия")
    hint: str = ""            # Подпись справа (горячая клавиша или пояснение)
    keywords: str = ""        # Дополнительные слова для поиска


def rank(command: Command, words: List[str]) -> Optional[int]:
    """Чем меньше — тем выше в списке; None — не подходит."""
    if not words:
        return 0
    title = command.title.lower()
    haystack = f"{title} {command.keywords.lower()} {command.group.lower()}"
    if not all(w in haystack for w in words):
        return None
    if title.startswith(words[0]):
        return 0
    if any(part.startswith(words[0]) for part in title.split()):
        return 1
    return 2 if words[0] in title else 3


class _PaletteDelegate(QStyledItemDelegate):
    def __init__(self, palette: "CommandPalette"):
        super().__init__(palette.list)
        self.palette = palette
        base = palette.list.font()
        self.font_title = QFont(base)
        self.font_title.setPointSizeF(base.pointSizeF() + 0.5)
        self.font_small = QFont(base)
        self.font_small.setPointSizeF(max(base.pointSizeF() - 1, 7.5))

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), 44)

    def paint(self, p: QPainter, option, index):
        command = self.palette.visible[index.data(INDEX_ROLE)]
        t = self.palette.tokens
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(6, 2, -6, -2)
        if option.state & QStyle.StateFlag.State_Selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(t["accent"], 0.14))
            p.drawRoundedRect(QRectF(r), 8, 8)
        p.drawPixmap(QRect(r.left() + 12, r.center().y() - 9, 18, 18),
                     icons.pixmap(command.icon, t["accent"] if option.state & QStyle.StateFlag.State_Selected
                                  else t["text_muted"], 18))
        fm_s = QFontMetrics(self.font_small)
        hint = command.hint or command.group
        hint_w = fm_s.horizontalAdvance(hint) + 4
        p.setFont(self.font_small)
        p.setPen(qcolor(t["text_faint"]))
        p.drawText(QRect(r.right() - hint_w - 10, r.top(), hint_w, r.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, hint)
        p.setFont(self.font_title)
        p.setPen(qcolor(t["text"]))
        width = r.width() - 56 - hint_w - 16
        p.drawText(QRect(r.left() + 44, r.top(), width, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   QFontMetrics(self.font_title).elidedText(command.title, Qt.TextElideMode.ElideRight, width))
        p.restore()


class CommandPalette(QFrame):
    """Всплывающая панель поверх окна. Команды собирает провайдер при каждом открытии."""

    def __init__(self, host: QWidget, tokens: dict, provider: Callable[[], List[Command]]):
        super().__init__(host)
        self.setObjectName("Palette")
        self.host = host
        self.tokens = tokens
        self.provider = provider
        self.commands: List[Command] = []
        self.visible: List[Command] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        self.input = QLineEdit()
        self.input.setObjectName("PaletteInput")
        self.input.setPlaceholderText(tr("Команда, сборка или мод…   (Esc — закрыть)"))
        self.input.textChanged.connect(self._filter)
        self.input.installEventFilter(self)
        layout.addWidget(self.input)
        self.list = QListWidget()
        self.list.setObjectName("PaletteList")
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setItemDelegate(_PaletteDelegate(self))
        self.list.itemActivated.connect(lambda _: self._run())
        self.list.itemClicked.connect(lambda _: self._run())
        layout.addWidget(self.list)
        add_shadow(self, tokens, 48, 0.55)
        self.hide()

    def set_tokens(self, tokens: dict):
        self.tokens = tokens
        add_shadow(self, tokens, 48, 0.55)

    def open(self):
        self.commands = self.provider()
        width = min(640, self.host.width() - 80)
        self.setFixedWidth(width)
        self.input.clear()
        self._filter("")
        self.move((self.host.width() - width) // 2, 70)
        self.show()
        self.raise_()
        self.input.setFocus()
        QApplication.instance().installEventFilter(self)  # Щелчок мимо панели закрывает её

    def hideEvent(self, event):
        QApplication.instance().removeEventFilter(self)
        super().hideEvent(event)

    def _filter(self, text: str):
        words = text.lower().split()
        ranked = [(rank(c, words), i, c) for i, c in enumerate(self.commands)]
        self.visible = [c for r, _, c in sorted((x for x in ranked if x[0] is not None), key=lambda x: (x[0], x[1]))][:MAX_RESULTS]
        self.list.clear()
        for i in range(len(self.visible)):
            item = QListWidgetItem()
            item.setData(INDEX_ROLE, i)
            self.list.addItem(item)
        if self.visible:
            self.list.setCurrentRow(0)
        rows_height = max(1, len(self.visible)) * 44 + 8
        self.list.setFixedHeight(min(rows_height, 440, max(120, self.host.height() - 200)))
        self.adjustSize()

    def _run(self):
        row = self.list.currentRow()
        if 0 <= row < len(self.visible):
            command = self.visible[row]
            self.hide()
            command.action()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.MouseButtonPress and self.isVisible():
            pos = self.mapFromGlobal(event.globalPosition().toPoint())
            if not self.rect().contains(pos):
                self.hide()
            return False
        if obj is self.input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Escape:
                self.hide()
                return True
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                step = 1 if key == Qt.Key.Key_Down else -1
                if self.visible:
                    self.list.setCurrentRow((self.list.currentRow() + step) % len(self.visible))
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._run()
                return True
        return super().eventFilter(obj, event)
