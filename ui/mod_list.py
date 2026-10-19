"""
Список модов: модель, фильтр/сортировка и делегат, рисующий строку целиком.
Один делегат вместо виджетов в каждой ячейке: список остаётся быстрым на сотнях модов.
"""
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import (
    QAbstractListModel, QEvent, QModelIndex, QPoint, QRect, QRectF, QSize, QSortFilterProxyModel, Qt, pyqtSignal,
)
from PyQt6.QtGui import QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QAbstractItemView, QListView, QStyle, QStyledItemDelegate, QToolTip

import icons
from enums import ModStatus
from theme import qcolor
from view_model import (
    CAT_ALL, matches_category, search_text, status_of, status_sort_key, status_style, subtitle_for,
)

ENTRY_ROLE = Qt.ItemDataRole.UserRole + 1
ROW_HEIGHT = 62


def entry_at(index: QModelIndex) -> Optional[Dict]:
    """
    Запись мода по индексу (в том числе индексу прокси) — сам словарь, а не копия.
    entry_at(index) возвращает копию: PyQt превращает dict в QVariantMap и обратно,
    поэтому изменения (например, отметка _is_checked) через неё терялись бы.
    """
    model = index.model() if index.isValid() else None
    while isinstance(model, QSortFilterProxyModel):
        index = model.mapToSource(index)
        model = index.model()
    return model.entry(index.row()) if isinstance(model, ModListModel) else None


class ModListModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: List[Dict] = []

    def set_items(self, items: List[Dict]):
        self.beginResetModel()
        self._items = list(items)
        self.endResetModel()

    def items(self) -> List[Dict]:
        return self._items

    def entry(self, row: int) -> Optional[Dict]:
        return self._items[row] if 0 <= row < len(self._items) else None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        entry = self._items[index.row()]
        if role == ENTRY_ROLE:
            return entry
        if role == Qt.ItemDataRole.DisplayRole:
            return entry.get("name", "")
        return None

    def refresh_where(self, predicate: Callable[[Dict], bool]):
        """Перерисовывает строки, для которых выполняется условие (без пересоздания модели)."""
        for row, entry in enumerate(self._items):
            if predicate(entry):
                idx = self.index(row)
                self.dataChanged.emit(idx, idx)

    def refresh_all(self):
        if self._items:
            self.dataChanged.emit(self.index(0), self.index(len(self._items) - 1))


class ModFilterProxy(QSortFilterProxyModel):
    SORT_NAME, SORT_STATUS = "name", "status"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.category = CAT_ALL
        self.query = ""
        self.sort_mode = self.SORT_STATUS
        self.setDynamicSortFilter(False)

    def set_filter(self, category: Optional[str] = None, query: Optional[str] = None):
        if category is not None:
            self.category = category
        if query is not None:
            self.query = query.strip().lower()
        self.invalidateFilter()

    def set_sort_mode(self, mode: str):
        self.sort_mode = mode
        self.invalidate()
        self.sort(0)

    def filterAcceptsRow(self, source_row, source_parent):
        entry = self.sourceModel().entry(source_row)
        if not entry or not matches_category(entry, self.category):
            return False
        return not self.query or all(word in search_text(entry) for word in self.query.split())

    def lessThan(self, left, right):
        a, b = self.sourceModel().entry(left.row()), self.sourceModel().entry(right.row())
        name_a, name_b = str(a.get("name", "")).lower(), str(b.get("name", "")).lower()
        if self.sort_mode == self.SORT_STATUS:
            return (status_sort_key(a), name_a) < (status_sort_key(b), name_b)
        return name_a < name_b


class ModDelegate(QStyledItemDelegate):
    """Рисует строку мода: чекбокс, иконка, название + подпись, версии, статус, кнопки."""
    check_toggled = pyqtSignal(object)
    update_clicked = pyqtSignal(object)
    menu_requested = pyqtSignal(object, QPoint)

    def __init__(self, view: QListView, tokens: dict, icon_provider: Callable[[Dict], Optional[QPixmap]]):
        super().__init__(view)
        self.view = view
        self.tokens = tokens
        self.icon_provider = icon_provider
        self._mouse = QPoint(-1, -1)
        base = view.font()
        self.font_name = QFont(base)
        self.font_name.setPointSizeF(base.pointSizeF() + 0.5)
        self.font_name.setWeight(QFont.Weight.DemiBold)
        self.font_small = QFont(base)
        self.font_small.setPointSizeF(max(base.pointSizeF() - 1, 7.5))
        self.font_chip = QFont(self.font_small)
        self.font_chip.setWeight(QFont.Weight.DemiBold)
        self.font_version = QFont(base)
        self.font_version_bold = QFont(base)
        self.font_version_bold.setWeight(QFont.Weight.DemiBold)

    def set_tokens(self, tokens: dict):
        self.tokens = tokens

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), ROW_HEIGHT)

    # --- Геометрия ---

    def _layout(self, rect: QRect) -> dict:
        r = rect.adjusted(14, 0, -10, 0)
        cy = r.center().y()
        lay = {"check": QRect(r.left(), cy - 9, 18, 18)}
        lay["icon"] = QRect(r.left() + 32, cy - 18, 36, 36)
        right = r.right()
        lay["more"] = QRect(right - 30, cy - 15, 30, 30)
        lay["update"] = QRect(lay["more"].left() - 34, cy - 15, 30, 30)
        lay["status"] = QRect(lay["update"].left() - 150, cy - 12, 140, 24)
        wide = rect.width() > 820
        version_width = 250 if wide else 0
        lay["versions"] = QRect(lay["status"].left() - version_width - 10, r.top(), version_width, r.height())
        text_left = lay["icon"].right() + 14
        text_right = (lay["versions"].left() if wide else lay["status"].left()) - 14
        lay["text"] = QRect(text_left, r.top(), max(40, text_right - text_left), r.height())
        lay["wide"] = wide
        return lay

    # --- Отрисовка ---

    def paint(self, painter: QPainter, option, index):
        entry = entry_at(index)
        if not entry:
            return
        t = self.tokens
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        lay = self._layout(rect)

        # Фон строки
        bg_rect = QRectF(rect.adjusted(4, 2, -4, -2))
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(t["accent"], 0.10))
            painter.drawRoundedRect(bg_rect, 9, 9)
            painter.setBrush(qcolor(t["accent"]))
            painter.drawRoundedRect(QRectF(rect.left() + 4, rect.top() + 14, 3, rect.height() - 28), 1.5, 1.5)
        elif hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(t["surface2"]))
            painter.drawRoundedRect(bg_rect, 9, 9)

        self._paint_checkbox(painter, lay["check"], bool(entry.get("_is_checked")), hovered)
        self._paint_icon(painter, lay["icon"], entry)
        self._paint_texts(painter, lay, entry)
        if lay["wide"]:
            self._paint_versions(painter, lay["versions"], entry)
        self._paint_status(painter, lay["status"], entry)

        status = status_of(entry)
        busy = status in (ModStatus.DOWNLOADING, ModStatus.FINISHING)
        if entry.get("download_url") and not busy:
            self._paint_round_button(painter, lay["update"], "download", accent=True,
                                     hot=lay["update"].contains(self._mouse) and hovered)
        if hovered or selected:
            self._paint_round_button(painter, lay["more"], "more", accent=False,
                                     hot=lay["more"].contains(self._mouse) and hovered)
        painter.restore()

    def _paint_checkbox(self, p: QPainter, r: QRect, checked: bool, hovered: bool):
        t = self.tokens
        rf = QRectF(r).adjusted(0.75, 0.75, -0.75, -0.75)
        if checked:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(t["accent"]))
            p.drawRoundedRect(rf, 5, 5)
            p.drawPixmap(r.adjusted(2, 2, -2, -2), icons.pixmap("check", t["accent_text"], 14))
        else:
            p.setPen(QPen(qcolor(t["accent"] if hovered else t["border_strong"]), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rf, 5, 5)

    def _paint_icon(self, p: QPainter, r: QRect, entry: Dict):
        t = self.tokens
        pm = self.icon_provider(entry)
        path = QPainterPath()
        path.addRoundedRect(QRectF(r), 9, 9)
        if pm and not pm.isNull():
            p.save()
            p.setClipPath(path)
            p.drawPixmap(r, pm)
            p.restore()
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(t["surface3"]))
            p.drawPath(path)
            p.setPen(qcolor(t["text_muted"]))
            p.setFont(self.font_name)
            letter = (str(entry.get("name") or "?").strip()[:1] or "?").upper()
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, letter)

    def _paint_texts(self, p: QPainter, lay: dict, entry: Dict):
        t = self.tokens
        r = lay["text"]
        name = str(entry.get("name") or "")
        p.setFont(self.font_name)
        p.setPen(qcolor(t["text"]))
        fm = QFontMetrics(self.font_name)
        name_rect = QRect(r.left(), r.top() + 11, r.width(), fm.height())
        p.drawText(name_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(name, Qt.TextElideMode.ElideRight, r.width()))

        subtitle, role = subtitle_for(entry)
        if not lay["wide"] and entry.get("current_version"):
            subtitle = self._versions_text(entry) + "  ·  " + subtitle
        p.setFont(self.font_small)
        p.setPen(qcolor(t[role] if role else t["text_muted"]))
        fm_small = QFontMetrics(self.font_small)
        sub_rect = QRect(r.left(), name_rect.bottom() + 3, r.width(), fm_small.height())
        p.drawText(sub_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   fm_small.elidedText(subtitle, Qt.TextElideMode.ElideRight, r.width()))

    @staticmethod
    def _versions_text(entry: Dict) -> str:
        current, new = entry.get("current_version") or "", entry.get("new_version") or ""
        return f"{current} → {new}" if current and new else (new or current)

    def _paint_versions(self, p: QPainter, r: QRect, entry: Dict):
        t = self.tokens
        current = str(entry.get("current_version") or "")
        new = str(entry.get("new_version") or "")
        if new:
            fm_b = QFontMetrics(self.font_version_bold)
            fm = QFontMetrics(self.font_version)
            new_w = min(fm_b.horizontalAdvance(new), r.width() // 2 + 20)
            arrow = "  →  " if current else ""
            arrow_w = fm.horizontalAdvance(arrow)
            cur_w = max(0, r.width() - new_w - arrow_w)
            x = r.left()
            if current:
                p.setFont(self.font_version)
                p.setPen(qcolor(t["text_muted"]))
                text = fm.elidedText(current, Qt.TextElideMode.ElideMiddle, cur_w)
                p.drawText(QRect(x, r.top(), cur_w, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
                x += fm.horizontalAdvance(text)
                p.setPen(qcolor(t["text_faint"]))
                p.drawText(QRect(x, r.top(), arrow_w, r.height()), Qt.AlignmentFlag.AlignVCenter, arrow)
                x += arrow_w
            p.setFont(self.font_version_bold)
            p.setPen(qcolor(t["accent"] if status_of(entry) == ModStatus.UPDATE_AVAILABLE else t["info"]))
            p.drawText(QRect(x, r.top(), r.right() - x, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       fm_b.elidedText(new, Qt.TextElideMode.ElideMiddle, r.right() - x))
        elif current:
            p.setFont(self.font_version)
            p.setPen(qcolor(t["text_muted"]))
            p.drawText(r, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       QFontMetrics(self.font_version).elidedText(current, Qt.TextElideMode.ElideMiddle, r.width()))

    def _paint_status(self, p: QPainter, r: QRect, entry: Dict):
        t = self.tokens
        progress = entry.get("_progress")
        status = status_of(entry)
        if status == ModStatus.DOWNLOADING:
            track = QRectF(r.left(), r.center().y() - 3, r.width(), 6)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(t["surface3"]))
            p.drawRoundedRect(track, 3, 3)
            if progress is not None:
                p.setBrush(qcolor(t["accent"]))
                p.drawRoundedRect(QRectF(track.left(), track.top(), max(6.0, track.width() * progress), 6), 3, 3)
                label = f"{int(progress * 100)}%"
            else:
                label = "Скачивание…"
            p.setFont(self.font_chip)
            p.setPen(qcolor(t["text_muted"]))
            p.drawText(QRect(r.left(), r.top() - 12, r.width(), 14), Qt.AlignmentFlag.AlignRight, label)
            return

        label, role = status_style(entry)
        color = t.get(role, t["text_muted"])
        fm = QFontMetrics(self.font_chip)
        width = min(r.width(), fm.horizontalAdvance(label) + 26)
        chip = QRectF(r.right() - width, r.top(), width, r.height())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcolor(color, 0.14))
        p.drawRoundedRect(chip, r.height() / 2, r.height() / 2)
        p.setBrush(qcolor(color))
        p.drawEllipse(QRectF(chip.left() + 9, chip.center().y() - 3, 6, 6))
        p.setFont(self.font_chip)
        p.setPen(qcolor(color))
        p.drawText(chip.adjusted(19, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)

    def _paint_round_button(self, p: QPainter, r: QRect, icon_name: str, accent: bool, hot: bool):
        t = self.tokens
        p.setPen(Qt.PenStyle.NoPen)
        if accent:
            p.setBrush(qcolor(t["accent"]) if hot else qcolor(t["accent"], 0.14))
            color = t["accent_text"] if hot else t["accent"]
        else:
            p.setBrush(qcolor(t["surface3"]) if hot else Qt.BrushStyle.NoBrush)
            color = t["text"] if hot else t["text_muted"]
        p.drawRoundedRect(QRectF(r), 8, 8)
        p.drawPixmap(r.adjusted(7, 7, -7, -7), icons.pixmap(icon_name, color, 16))

    # --- Взаимодействие ---

    def editorEvent(self, event, model, option, index):
        entry = entry_at(index)
        if not entry:
            return False
        etype = event.type()
        if etype == QEvent.Type.MouseMove:
            self._mouse = event.position().toPoint()
            self.view.viewport().update(option.rect)
            return False
        if etype not in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick):
            return False
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        pos = event.position().toPoint()
        lay = self._layout(option.rect)
        hit_check = lay["check"].adjusted(-8, -12, 8, 12).contains(pos)
        hit_update = bool(entry.get("download_url")) and lay["update"].contains(pos)
        hit_more = lay["more"].contains(pos)
        if not (hit_check or hit_update or hit_more):
            return False
        if etype == QEvent.Type.MouseButtonRelease:
            if hit_check:
                entry["_is_checked"] = not entry.get("_is_checked")
                self.view.viewport().update(option.rect)
                self.check_toggled.emit(entry)
            elif hit_update:
                self.update_clicked.emit(entry)
            elif hit_more:
                self.menu_requested.emit(entry, self.view.viewport().mapToGlobal(lay["more"].bottomLeft()))
        return True  # Клики по элементам управления не меняют выделение строки

    def helpEvent(self, event, view, option, index):
        entry = entry_at(index)
        if not entry or event.type() != QEvent.Type.ToolTip:
            return super().helpEvent(event, view, option, index)
        pos = event.pos()
        lay = self._layout(option.rect)
        tip = None
        if entry.get("download_url") and lay["update"].contains(pos):
            verb = "Установить" if not entry.get("filepath") else "Обновить до"
            tip = f"{verb} {entry.get('new_version') or ''}".strip()
        elif lay["more"].contains(pos):
            tip = "Действия"
        elif lay["check"].adjusted(-8, -12, 8, 12).contains(pos):
            tip = "Отметить для массовых действий"
        elif lay["status"].contains(pos):
            tip = str(entry.get("status") or "")
        elif lay["text"].contains(pos):
            subtitle, _ = subtitle_for(entry)
            tip = f"<b>{entry.get('name', '')}</b><br>{subtitle}"
        if tip:
            QToolTip.showText(event.globalPos(), tip, view)
            return True
        QToolTip.hideText()
        return True


class ModListView(QListView):
    """Список с плавной прокруткой и подсветкой строки под курсором."""
    context_menu_requested = pyqtSignal(object, QPoint)
    toggle_requested = pyqtSignal()
    delete_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ModList")
        self.setMouseTracking(True)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(18)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSpacing(0)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def contextMenuEvent(self, event):
        index = self.indexAt(event.pos())
        entry = entry_at(index)
        if entry:
            self.setCurrentIndex(index)
            self.context_menu_requested.emit(entry, event.globalPos())

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space:
            self.toggle_requested.emit()
            return
        if event.key() == Qt.Key.Key_Delete:
            self.delete_requested.emit()
            return
        super().keyPressEvent(event)

    def leaveEvent(self, event):
        delegate = self.itemDelegate()
        if isinstance(delegate, ModDelegate):
            delegate._mouse = QPoint(-1, -1)
        self.viewport().update()
        super().leaveEvent(event)

