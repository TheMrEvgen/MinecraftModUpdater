"""
Галерея скриншотов проекта: лента миниатюр (листается колесом, стрелками по краям и плавно) и просмотр
картинки целиком с переключением между скриншотами (кнопки, ← / →, колесо, щелчок по картинке).
"""
from typing import Dict, List, Optional

from PyQt6.QtCore import QEasingCurve, QEvent, QPropertyAnimation, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

import icons
from ui.dialogs import FramelessDialog
from ui.widgets import label, rounded_pixmap
from i18n import plural, tr

THUMB = QSize(168, 96)
MAX_ITEMS = 16


def gallery_items(gallery: Optional[List[Dict]]) -> List[Dict]:
    """Сначала «главная» картинка, остальные — в порядке проекта."""
    return sorted(gallery or [], key=lambda g: not g.get("featured"))[:MAX_ITEMS]


class _Thumb(QLabel):
    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(THUMB)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("Thumb")

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


def _arrow(icon_name: str, parent: QWidget) -> QPushButton:
    btn = QPushButton(parent)
    btn.setObjectName("GalleryArrow")
    btn.setFixedSize(32, 32)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setProperty("icon_name", icon_name)
    return btn


class GalleryStrip(QWidget):
    """Лента миниатюр. Картинки грузит общий загрузчик (IconLoader с размером под миниатюры)."""

    def __init__(self, thumb_loader, full_loader, parent=None):
        super().__init__(parent)
        self.thumb_loader = thumb_loader
        self.full_loader = full_loader
        self.items: List[Dict] = []
        self.thumbs: Dict[str, _Thumb] = {}
        self.tokens: Optional[dict] = None
        self.setFixedHeight(THUMB.height() + 42)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFixedHeight(THUMB.height() + 4)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.viewport().installEventFilter(self)  # Колесо мыши листает ленту вбок
        self.container = QWidget()
        self.row = QHBoxLayout(self.container)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(8)
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll)
        self.hint = label("", "Faint", wrap=True)
        layout.addWidget(self.hint)

        self.left = _arrow("chevron_left", self)
        self.right = _arrow("chevron_right", self)
        self.left.clicked.connect(lambda: self._step(-1))
        self.right.clicked.connect(lambda: self._step(1))
        self.scroll.horizontalScrollBar().valueChanged.connect(self._update_arrows)
        self.scroll.horizontalScrollBar().rangeChanged.connect(lambda *_: self._update_arrows())
        self._animation = QPropertyAnimation(self.scroll.horizontalScrollBar(), b"value", self)
        self._animation.setDuration(220)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        thumb_loader.icon_ready.connect(self._on_loaded)

    def set_tokens(self, tokens: dict):
        self.tokens = tokens
        for btn in (self.left, self.right):
            btn.setIcon(icons.icon(btn.property("icon_name"), tokens["text"], 18))

    def set_items(self, gallery: List[Dict]):
        while self.row.count():
            item = self.row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.thumbs.clear()
        self.items = gallery_items(gallery)
        for index, item in enumerate(self.items):
            thumb = _Thumb()
            thumb.setToolTip(item.get("title") or tr("Открыть скриншот"))
            thumb.clicked.connect(lambda i=index: self._open(i))
            key = self.thumb_loader.url_key(item["url"])
            self.thumbs[key] = thumb
            path = self.thumb_loader.request_url(item["url"])
            if path:
                self._show(thumb, path)
            self.row.addWidget(thumb)
        self.row.addStretch()
        self.scroll.horizontalScrollBar().setValue(0)
        count = len(self.items)
        self.hint.setText(tr('{0} {1} · колесо мыши или стрелки — листать, щелчок — открыть', count, plural(count, 'скриншот', 'скриншота', 'скриншотов')))
        self.setVisible(bool(self.items))
        self._update_arrows()

    # --- Листание ---

    def _step(self, direction: int):
        bar = self.scroll.horizontalScrollBar()
        page = max(THUMB.width() + 8, (self.scroll.viewport().width() // (THUMB.width() + 8)) * (THUMB.width() + 8))
        target = max(0, min(bar.maximum(), (self._animation.endValue() if self._animation.state() ==
                                            QPropertyAnimation.State.Running else bar.value()) + direction * page))
        self._animation.stop()
        self._animation.setStartValue(bar.value())
        self._animation.setEndValue(target)
        self._animation.start()

    def eventFilter(self, obj, event):
        if obj is self.scroll.viewport() and event.type() == QEvent.Type.Wheel:
            delta = event.angleDelta().y() or event.angleDelta().x()
            if self.scroll.horizontalScrollBar().maximum() > 0 and delta:
                self._step(-1 if delta > 0 else 1)
                return True
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        y = (self.scroll.height() - self.left.height()) // 2
        self.left.move(6, y)
        self.right.move(self.width() - self.right.width() - 6, y)
        self._update_arrows()

    def _update_arrows(self, *_):
        bar = self.scroll.horizontalScrollBar()
        self.left.setVisible(bar.value() > 0)
        self.right.setVisible(bar.value() < bar.maximum())
        self.left.raise_()
        self.right.raise_()

    # --- Картинки ---

    def _show(self, thumb: _Thumb, path: str):
        pm = QPixmap(path)
        if not pm.isNull():
            pm = pm.scaled(THUMB, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
            x, y = (pm.width() - THUMB.width()) // 2, (pm.height() - THUMB.height()) // 2
            thumb.setPixmap(rounded_pixmap(pm.copy(x, y, THUMB.width(), THUMB.height()), THUMB.width(), 10))

    def _on_loaded(self, key: str, path: str):
        thumb = self.thumbs.get(key)
        if thumb:
            self._show(thumb, path)

    def _open(self, index: int):
        ImageViewer(self.window(), self.full_loader, self.items, index, self.tokens).exec()


class ImageViewer(FramelessDialog):
    """Скриншот целиком; ← / → (кнопки, клавиши, колесо) — соседние, щелчок по картинке — следующий."""

    def __init__(self, parent, loader, items: List[Dict], index: int = 0, tokens: Optional[dict] = None):
        super().__init__(parent, tr("Скриншоты"), 720)
        self.loader = loader
        self.items = items
        self.index = index
        self.tokens = tokens or {"text": "#e9ecf2"}
        self._pixmap: Optional[QPixmap] = None

        row = QHBoxLayout()
        row.setSpacing(10)
        self.prev = button_arrow("chevron_left", self.tokens, tr("Предыдущий (←)"))
        self.next = button_arrow("chevron_right", self.tokens, tr("Следующий (→)"))
        self.prev.clicked.connect(lambda: self.go(-1))
        self.next.clicked.connect(lambda: self.go(1))
        self.image = QLabel(tr("Загрузка…"))
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumSize(640, 360)
        self.image.setCursor(Qt.CursorShape.PointingHandCursor)
        self.image.installEventFilter(self)
        row.addWidget(self.prev, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.image, 1)
        row.addWidget(self.next, 0, Qt.AlignmentFlag.AlignVCenter)
        self.body.addLayout(row, 1)
        self.caption = label("", "Muted", wrap=True)
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.addWidget(self.caption)
        self.counter = label("", "Faint")
        self.buttons.insertWidget(0, self.counter)
        self.add_button(tr("Закрыть"), True, self.accept)
        self.finish_layout()
        loader.icon_ready.connect(self._on_loaded)
        self._fit_to_screen()
        self.show_index(index)

    def _fit_to_screen(self):
        screen = self.screen().availableGeometry() if self.screen() else None
        if screen:
            self.resize(int(screen.width() * 0.8), int(screen.height() * 0.82))

    def _url(self, index: int) -> str:
        item = self.items[index]
        return item.get("raw_url") or item.get("url")

    def show_index(self, index: int):
        if not self.items:
            return
        self.index = index % len(self.items)
        item = self.items[self.index]
        self.caption.setText(item.get("title") or "")
        self.caption.setVisible(bool(item.get("title")))
        self.counter.setText(f"{self.index + 1} / {len(self.items)}")
        multiple = len(self.items) > 1
        self.prev.setVisible(multiple)
        self.next.setVisible(multiple)
        self._pixmap = None
        self.image.setPixmap(QPixmap())
        self.image.setText(tr("Загрузка…"))
        path = self.loader.request_url(self._url(self.index))
        if path:
            self._set(path)
        for neighbour in (self.index - 1, self.index + 1):  # Соседние грузим заранее — листается без ожидания
            if multiple:
                self.loader.request_url(self._url(neighbour % len(self.items)))

    def go(self, step: int):
        self.show_index(self.index + step)

    def _on_loaded(self, key: str, path: str):
        if self.items and key == self.loader.url_key(self._url(self.index)):
            self._set(path)

    def _set(self, path: str):
        pm = QPixmap(path)
        if pm.isNull():
            self.image.setText(tr("Не удалось загрузить картинку"))
            return
        self._pixmap = pm
        self._rescale()

    def _rescale(self):
        if not self._pixmap:
            return
        area = self.image.size()
        self.image.setPixmap(self._pixmap.scaled(area.boundedTo(self._pixmap.size()) if self._pixmap.width() < area.width()
                                                 else area, Qt.AspectRatioMode.KeepAspectRatio,
                                                 Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_A):
            self.go(-1)
        elif event.key() in (Qt.Key.Key_Right, Qt.Key.Key_D, Qt.Key.Key_Space):
            self.go(1)
        else:
            super().keyPressEvent(event)

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta:
            self.go(-1 if delta > 0 else 1)

    def eventFilter(self, obj, event):
        if obj is self.image and event.type() == QEvent.Type.MouseButtonRelease and \
                event.button() == Qt.MouseButton.LeftButton and len(self.items) > 1:
            self.go(1)
            return True
        return super().eventFilter(obj, event)


def button_arrow(icon_name: str, tokens: dict, tooltip: str) -> QPushButton:
    btn = QPushButton()
    btn.setObjectName("GalleryArrow")
    btn.setFixedSize(40, 40)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setToolTip(tooltip)
    btn.setIcon(icons.icon(icon_name, tokens["text"], 20))
    return btn


class LinksRow(QWidget):
    """Ряд ссылок проекта: исходники, баг-трекер, вики, Discord, поддержка автора."""

    def __init__(self, tokens: dict, parent=None):
        super().__init__(parent)
        self.tokens = tokens
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)

    def set_links(self, links: List[tuple]):
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        from ui.widgets import flex_button
        while self.layout_.count():
            item = self.layout_.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    child = item.layout().takeAt(0)
                    if child.widget():
                        child.widget().deleteLater()
        row: Optional[QHBoxLayout] = None
        for i, (title, icon_name, url) in enumerate(links or []):
            if i % 2 == 0:
                row = QHBoxLayout()
                row.setSpacing(8)
                self.layout_.addLayout(row)
            btn = flex_button(title, None, icon_name, self.tokens, "text", f"{title}: {url}")
            btn.clicked.connect(lambda _=False, u=url: QDesktopServices.openUrl(QUrl(u)))
            row.addWidget(btn)
        if links and len(links) % 2 == 1:
            row.addStretch()
        self.setVisible(bool(links))
