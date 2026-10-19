"""Страница «Каталог»: поиск модов, шейдеров и ресурспаков на Modrinth и установка в один клик."""
from typing import Callable, Dict, List, Optional, Set

from PyQt6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QFontMetrics, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QComboBox, QHBoxLayout, QListWidget, QListWidgetItem,
    QStackedWidget, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

import icons
from content import CONTENT_TYPES, MODS, ContentType
from mod_updater_logic import search_projects
from theme import qcolor
from ui.widgets import EmptyState, SearchBox, button, label, refresh_button_icons
from view_model import CATEGORY_LABELS, LOADER_CATEGORIES

HIT_ROLE = Qt.ItemDataRole.UserRole + 1   # индекс в CatalogPage.hits
PAGE_SIZE = 20
SORTS = (("relevance", "По релевантности"), ("downloads", "Популярные"), ("updated", "Недавно обновлённые"),
         ("newest", "Новые"))
CONTENT_ICONS = {"mods": "package", "shaders": "sparkles", "resourcepacks": "image"}


def format_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} млн".replace(".", ",").replace(",0 ", " ")
    if n >= 1_000:
        return f"{n / 1_000:.0f} тыс."
    return str(n)


class CatalogDelegate(QStyledItemDelegate):
    """Карточка проекта: иконка, название, автор, описание, загрузки и метки, кнопка установки."""
    ROW_HEIGHT = 92

    def __init__(self, page: "CatalogPage"):
        super().__init__(page.list)
        self.page = page
        self._mouse = QPoint(-1, -1)
        base = page.list.font()
        self.font_title = QFont(base)
        self.font_title.setPointSizeF(base.pointSizeF() + 1)
        self.font_title.setWeight(QFont.Weight.DemiBold)
        self.font_small = QFont(base)
        self.font_small.setPointSizeF(max(base.pointSizeF() - 1, 7.5))
        self.font_button = QFont(base)
        self.font_button.setWeight(QFont.Weight.DemiBold)

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), self.ROW_HEIGHT)

    @staticmethod
    def button_rect(rect: QRect) -> QRect:
        return QRect(rect.right() - 150, rect.center().y() - 17, 134, 34)

    def paint(self, p: QPainter, option, index):
        hit = self.page.hit_at(index.row())
        if not hit:
            return
        t = self.page.tokens
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if hovered:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(t["surface2"]))
            p.drawRoundedRect(QRectF(rect.adjusted(4, 3, -4, -3)), 10, 10)

        icon_rect = QRect(rect.left() + 16, rect.center().y() - 26, 52, 52)
        path = QPainterPath()
        path.addRoundedRect(QRectF(icon_rect), 12, 12)
        pm = self.page.icon_provider({"project_id": hit.get("project_id"), "icon_url": hit.get("icon_url")})
        if pm and not pm.isNull():
            p.setClipPath(path)
            p.drawPixmap(icon_rect, pm)
            p.setClipping(False)
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(t["surface3"]))
            p.drawPath(path)
            p.drawPixmap(icon_rect.adjusted(14, 14, -14, -14), icons.pixmap("package", t["text_faint"], 24))

        btn = self.button_rect(rect)
        left = icon_rect.right() + 16
        width = btn.left() - 16 - left
        fm_t, fm_s = QFontMetrics(self.font_title), QFontMetrics(self.font_small)
        y = rect.top() + 12
        title = hit.get("title") or ""
        p.setFont(self.font_title)
        p.setPen(qcolor(t["text"]))
        title = fm_t.elidedText(title, Qt.TextElideMode.ElideRight, width)
        p.drawText(QRect(left, y, width, fm_t.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        author = f"от {hit.get('author')}" if hit.get("author") else ""
        if author:
            x = left + fm_t.horizontalAdvance(title) + 8
            p.setFont(self.font_small)
            p.setPen(qcolor(t["text_faint"]))
            p.drawText(QRect(x, y, max(0, left + width - x), fm_t.height()),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       fm_s.elidedText(author, Qt.TextElideMode.ElideRight, max(0, left + width - x)))
        y += fm_t.height() + 3
        p.setFont(self.font_small)
        p.setPen(qcolor(t["text_muted"]))
        p.drawText(QRect(left, y, width, fm_s.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   fm_s.elidedText(hit.get("description") or "", Qt.TextElideMode.ElideRight, width))
        y += fm_s.height() + 7
        x = left
        p.drawPixmap(QRect(x, y + 1, 12, 12), icons.pixmap("download", t["text_faint"], 12))
        p.setPen(qcolor(t["text_faint"]))
        text = format_count(hit.get("downloads") or 0)
        p.drawText(QRect(x + 16, y, 100, fm_s.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        x += 16 + fm_s.horizontalAdvance(text) + 14
        for cat in [c for c in hit.get("display_categories") or hit.get("categories") or [] if c not in LOADER_CATEGORIES][:4]:
            tag = CATEGORY_LABELS.get(cat, cat)
            w = fm_s.horizontalAdvance(tag) + 14
            if x + w > left + width:
                break
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(t["surface3"]))
            p.drawRoundedRect(QRectF(x, y - 1, w, fm_s.height() + 2), 8, 8)
            p.setPen(qcolor(t["text_muted"]))
            p.drawText(QRect(int(x), y - 1, w, fm_s.height() + 2), Qt.AlignmentFlag.AlignCenter, tag)
            x += w + 6

        state = self.page.install_state(hit)
        hot = hovered and btn.contains(self._mouse)
        p.setFont(self.font_button)
        if state == "installed":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(t["accent"], 0.12))
            p.drawRoundedRect(QRectF(btn), 9, 9)
            p.setPen(qcolor(t["accent"]))
            p.drawText(btn, Qt.AlignmentFlag.AlignCenter, "✓ Установлен")
        elif state == "installing":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(t["surface3"]))
            p.drawRoundedRect(QRectF(btn), 9, 9)
            p.setPen(qcolor(t["text_muted"]))
            p.drawText(btn, Qt.AlignmentFlag.AlignCenter, "Установка…")
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(t["accent_hover"] if hot else t["accent"]))
            p.drawRoundedRect(QRectF(btn), 9, 9)
            p.drawPixmap(QRect(btn.left() + 16, btn.center().y() - 8, 16, 16), icons.pixmap("download", t["accent_text"], 16))
            p.setPen(qcolor(t["accent_text"]))
            p.drawText(btn.adjusted(26, 0, 0, 0), Qt.AlignmentFlag.AlignCenter, "Установить")
        p.restore()

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.Type.MouseMove:
            self._mouse = event.position().toPoint()
            self.page.list.viewport().update()
            return False
        if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            if self.button_rect(option.rect).contains(event.position().toPoint()):
                hit = self.page.hit_at(index.row())
                if hit and self.page.install_state(hit) == "available":
                    self.page.install_requested.emit(hit)
                return True
        return False


class CatalogPage(QWidget):
    install_requested = pyqtSignal(object)    # hit из ответа /search
    open_requested = pyqtSignal(object)

    def __init__(self, tokens: dict, run_task: Callable, icon_provider: Callable[[Dict], Optional[QPixmap]], parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.tokens = tokens
        self.run_task = run_task
        self.icon_provider = icon_provider
        self.content: ContentType = MODS
        self.game_version = ""
        self.loader = ""
        self.installed: Dict[str, Set[str]] = {}     # тип контента -> project_id установленных
        self.installing: Set[str] = set()
        self.hits: List[Dict] = []
        self.total = 0
        self._request = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 18)
        root.setSpacing(14)
        head = QVBoxLayout()
        head.setSpacing(4)
        head.addWidget(label("Каталог", "PageTitle"))
        self.context_label = label("", "PageSubtitle")
        head.addWidget(self.context_label)
        root.addLayout(head)

        tabs = QHBoxLayout()
        tabs.setSpacing(6)
        self.tab_group = QButtonGroup(self)
        for i, content in enumerate(CONTENT_TYPES):
            tab = button(f" {content.title}", "Segment", CONTENT_ICONS[content.key], tokens, "text_muted")
            tab.setCheckable(True)
            tab.setProperty("content_key", content.key)
            self.tab_group.addButton(tab, i)
            tabs.addWidget(tab)
        self.tab_group.buttons()[0].setChecked(True)
        self.tab_group.idClicked.connect(lambda i: self._set_content(CONTENT_TYPES[i]))
        tabs.addStretch()
        root.addLayout(tabs)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.search = SearchBox(tokens, "Найти на Modrinth: название, описание…")
        self.search.setMinimumHeight(36)
        self._timer = QTimer(self, singleShot=True, interval=350)
        self._timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda: self._timer.start())
        self.search.returnPressed.connect(self.refresh)
        toolbar.addWidget(self.search, 1)
        self.sort_combo = QComboBox()
        for key, title in SORTS:
            self.sort_combo.addItem(title, key)
        self.sort_combo.setMinimumHeight(36)
        self.sort_combo.currentIndexChanged.connect(self.refresh)
        toolbar.addWidget(self.sort_combo)
        root.addLayout(toolbar)

        self.stack = QStackedWidget()
        self.list = QListWidget()
        self.list.setObjectName("ModList")
        self.list.setMouseTracking(True)
        self.list.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.verticalScrollBar().setSingleStep(18)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list.setItemDelegate(CatalogDelegate(self))
        self.list.itemDoubleClicked.connect(lambda item: self.open_requested.emit(self.hit_at(self.list.row(item))))
        self.list.verticalScrollBar().valueChanged.connect(self._maybe_load_more)
        self.stack.addWidget(self.list)
        self.empty = EmptyState()
        self.stack.addWidget(self.empty)
        root.addWidget(self.stack, 1)
        self.status = label("", "Faint")
        root.addWidget(self.status)

    # --- Состояние ---

    def set_tokens(self, tokens: dict):
        self.tokens = tokens
        self.search.set_tokens(tokens)
        refresh_button_icons(self, tokens)
        self.list.viewport().update()

    def set_context(self, game_version: str, loader: str, installed: Dict[str, Set[str]]):
        changed = (game_version, loader) != (self.game_version, self.loader)
        self.game_version, self.loader, self.installed = game_version, loader, installed
        loader_title = {"fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge", "quilt": "Quilt"}.get(loader, loader)
        filters = f"Minecraft {game_version or '?'}" + (f" · {loader_title}" if self.content is MODS and loader else "")
        self.context_label.setText(f"Моды, шейдеры и ресурспаки с Modrinth для текущей сборки: {filters}. "
                                   "Обязательные зависимости ставятся автоматически.")
        if changed and self.isVisible():
            self.refresh()
        self.list.viewport().update()

    def hit_at(self, row: int) -> Optional[Dict]:
        return self.hits[row] if 0 <= row < len(self.hits) else None

    def install_state(self, hit: Dict) -> str:
        pid = hit.get("project_id")
        if pid in self.installing:
            return "installing"
        if pid in self.installed.get(self.content.key, set()):
            return "installed"
        return "available"

    def set_installing(self, project_ids, installing: bool):
        for pid in project_ids:
            (self.installing.add if installing else self.installing.discard)(pid)
        self.list.viewport().update()

    def icon_updated(self):
        self.list.viewport().update()

    def _set_content(self, content: ContentType):
        self.content = content
        self.set_context(self.game_version, self.loader, self.installed)
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        if not self.hits:
            self.refresh()

    # --- Поиск ---

    def refresh(self):
        self._request += 1
        self.hits, self.total = [], 0
        self.list.clear()
        self._search(0)

    def _maybe_load_more(self, value: int):
        bar = self.list.verticalScrollBar()
        if value >= bar.maximum() - 200 and len(self.hits) < self.total and not getattr(self, "_loading", False):
            self._search(len(self.hits))

    def _search(self, offset: int):
        if not self.game_version:
            self._show_empty("alert", "Не указана версия игры", "Выберите сборку с известной версией Minecraft.")
            return
        request = self._request
        self._loading = True
        self.status.setText("Ищем на Modrinth…")
        loaders = self.content.loaders(self.loader) if self.content is MODS else None
        args = (self.search.text().strip(), self.content.project_type, self.game_version, loaders,
                self.sort_combo.currentData(), offset, PAGE_SIZE)
        self.run_task(search_projects, args, lambda result: self._on_results(request, result),
                      lambda error: self._on_error(request, error), None)

    def _on_results(self, request: int, result: Dict):
        if request != self._request:
            return  # Ответ на устаревший запрос
        self._loading = False
        hits = result.get("hits") or []
        self.total = int(result.get("total_hits") or 0)
        for hit in hits:
            item = QListWidgetItem()
            item.setData(HIT_ROLE, len(self.hits))
            self.hits.append(hit)
            self.list.addItem(item)
        if not self.hits:
            self._show_empty("search", "Ничего не найдено", "Попробуйте другой запрос.")
        else:
            self.stack.setCurrentIndex(0)
        self.status.setText(f"Найдено: {format_count(self.total)}  ·  показано {len(self.hits)}")

    def _on_error(self, request: int, error: str):
        if request != self._request:
            return
        self._loading = False
        self.status.setText("")
        self._show_empty("error", "Не удалось выполнить поиск", f"Проверьте подключение к интернету.\n{error}")

    def _show_empty(self, icon_name: str, title: str, text: str):
        self.empty.set_content(self.tokens, icon_name, title, text)
        self.stack.setCurrentIndex(1)
