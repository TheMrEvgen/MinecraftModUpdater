"""Галерея скриншотов проекта: лента миниатюр и просмотр картинки целиком."""
from typing import Dict, List, Optional

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from ui.dialogs import FramelessDialog
from ui.widgets import rounded_pixmap

THUMB = QSize(168, 96)


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


class GalleryStrip(QScrollArea):
    """Горизонтальная лента миниатюр. Картинки грузит общий загрузчик (IconLoader с размером под миниатюры)."""

    def __init__(self, thumb_loader, full_loader, parent=None):
        super().__init__(parent)
        self.thumb_loader = thumb_loader
        self.full_loader = full_loader
        self.items: List[Dict] = []
        self.thumbs: Dict[str, _Thumb] = {}
        self.setWidgetResizable(True)
        self.setFixedHeight(THUMB.height() + 18)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.container = QWidget()
        self.row = QHBoxLayout(self.container)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(8)
        self.setWidget(self.container)
        thumb_loader.icon_ready.connect(self._on_loaded)

    def set_items(self, gallery: List[Dict]):
        while self.row.count():
            item = self.row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.thumbs.clear()
        # Сначала «главная» картинка, остальные — как в проекте
        self.items = sorted(gallery or [], key=lambda g: not g.get("featured"))[:12]
        for item in self.items:
            thumb = _Thumb()
            thumb.setToolTip(item.get("title") or "")
            thumb.clicked.connect(lambda i=item: self._open(i))
            key = self.thumb_loader.url_key(item["url"])
            self.thumbs[key] = thumb
            path = self.thumb_loader.request_url(item["url"])
            if path:
                self._show(thumb, path)
            self.row.addWidget(thumb)
        self.row.addStretch()
        self.setVisible(bool(self.items))

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

    def _open(self, item: Dict):
        ImageViewer(self.window(), self.full_loader, item).exec()


class ImageViewer(FramelessDialog):
    """Картинка галереи целиком (грузится в большом размере)."""

    def __init__(self, parent, loader, item: Dict):
        super().__init__(parent, item.get("title") or "Скриншот", 640)
        self.loader = loader
        self.url = item.get("raw_url") or item.get("url")
        self.key = loader.url_key(self.url)
        self.image = QLabel("Загрузка…")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumSize(640, 360)
        self.body.addWidget(self.image, 1)
        self.add_button("Закрыть", True, self.accept)
        self.finish_layout()
        loader.icon_ready.connect(self._on_loaded)
        path = loader.request_url(self.url)
        if path:
            self._show(path)

    def _on_loaded(self, key: str, path: str):
        if key == self.key:
            self._show(path)

    def _show(self, path: str):
        pm = QPixmap(path)
        if pm.isNull():
            return
        screen = self.screen().availableGeometry() if self.screen() else None
        limit = QSize(int(screen.width() * 0.8), int(screen.height() * 0.75)) if screen else QSize(1200, 800)
        self.image.setPixmap(pm.scaled(limit.boundedTo(pm.size()), Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation))
        self.adjustSize()


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
        from ui.widgets import button
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
            btn = button(title, None, icon_name, self.tokens, "text", url)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(lambda _=False, u=url: QDesktopServices.openUrl(QUrl(u)))
            row.addWidget(btn)
        if links and len(links) % 2 == 1:
            row.addStretch()
        self.setVisible(bool(links))
