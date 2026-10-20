"""
Список модов: модель, фильтр/сортировка и делегат, рисующий строку целиком.
Один делегат вместо виджетов в каждой ячейке: список остаётся быстрым на сотнях модов.
"""
import datetime
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import (
    QAbstractListModel, QEvent, QModelIndex, QPoint, QRect, QRectF, QSize, QSortFilterProxyModel, Qt, pyqtSignal,
)
from PyQt6.QtGui import QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QAbstractItemView, QListView, QStyle, QStyledItemDelegate, QToolTip

import icons
from enums import ModStatus
from theme import qcolor
from mod_updater_logic import format_date
from query import Query, is_abandoned, parse_query
from view_model import (
    CAT_ALL, DEP_TYPE_LABELS, GROUP_TITLES, entry_key, entry_tags, format_size, group_of, matches_category,
    side_tag, status_of, status_sort_key, status_style, subtitle_for, update_date,
)
from i18n import tr

ENTRY_ROLE = Qt.ItemDataRole.UserRole + 1
ROW_HEIGHT = 62
ROW_HEIGHT_COMPACT = 44
TREE_INDENT = 30  # Сдвиг строки зависимости в режиме дерева
HEADER_HEIGHT = 36  # Заголовок группы над первой строкой группы
# Что можно показывать в строке мода (настройка «Колонки»)
COLUMNS = (("description", tr("Описание")), ("versions", tr("Версии")), ("size", tr("Размер")), ("date", tr("Дата обновления")),
           ("side", tr("Сторона (клиент/сервер)")), ("tags", tr("Метки")))
DEFAULT_COLUMNS = ("description", "versions")
META_KEYS = ("size", "date", "side")  # Порядок колонок справа
META_WIDTHS = {"size": 76, "date": 96, "side": 120}
SIDE_SHORT = {"side:client": tr("клиент"), "side:server": tr("сервер"), "side:both": tr("клиент + сервер"), "side:any": tr("любая")}


def _timestamp(iso: str) -> float:
    try:
        return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else 0.0
    except ValueError:
        return 0.0


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


def meta_at(index: QModelIndex) -> Dict:
    """Положение строки в дереве: depth, parent_row, dep_type, children (см. view_model.build_rows)."""
    model = index.model() if index.isValid() else None
    while isinstance(model, QSortFilterProxyModel):
        index = model.mapToSource(index)
        model = index.model()
    return model.meta(index.row()) if isinstance(model, ModListModel) else {}


class ModListModel(QAbstractListModel):
    """Строки списка. В режиме дерева одна запись может стоять в нескольких строках (общая зависимость)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: List[tuple] = []
        self._items: List[Dict] = []

    def set_items(self, items: List[Dict]):
        self.set_rows([(e, {"depth": 0}) for e in items])

    def set_rows(self, rows: List[tuple]):
        self.beginResetModel()
        self._rows = list(rows)
        seen, self._items = set(), []
        for entry, _ in self._rows:
            if id(entry) not in seen:
                seen.add(id(entry))
                self._items.append(entry)
        self.endResetModel()

    def items(self) -> List[Dict]:
        """Записи без повторов (для отметок и массовых действий)."""
        return self._items

    def entry(self, row: int) -> Optional[Dict]:
        return self._rows[row][0] if 0 <= row < len(self._rows) else None

    def meta(self, row: int) -> Dict:
        return self._rows[row][1] if 0 <= row < len(self._rows) else {}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        entry = self._rows[index.row()][0]
        if role == ENTRY_ROLE:
            return entry
        if role == Qt.ItemDataRole.DisplayRole:
            return entry.get("name", "")
        return None

    def refresh_where(self, predicate: Callable[[Dict], bool]):
        """Перерисовывает строки, для которых выполняется условие (без пересоздания модели)."""
        for row, (entry, _) in enumerate(self._rows):
            if predicate(entry):
                idx = self.index(row)
                self.dataChanged.emit(idx, idx)

    def refresh_all(self):
        if self._rows:
            self.dataChanged.emit(self.index(0), self.index(len(self._rows) - 1))


class ModFilterProxy(QSortFilterProxyModel):
    SORT_NAME, SORT_STATUS, SORT_UPDATED, SORT_SIZE = "name", "status", "updated", "size"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.category = CAT_ALL
        self.query = ""
        self._query: Query = Query()        # Разобранная строка поиска (см. query.py)
        self.tag: Optional[str] = None      # Фильтр по метке (ключ из view_model.entry_tags)
        self.custom_tags: Dict = {}         # Метки пользователя (для поиска и фильтра)
        self.sort_mode = self.SORT_STATUS
        self._group_counts: Optional[Dict[str, int]] = None
        for signal in (self.layoutChanged, self.modelReset, self.rowsInserted, self.rowsRemoved):
            signal.connect(self._clear_group_cache)
        self.expand_all = False   # Режим дерева: по умолчанию раскрыты все или ни один
        self.toggled: set = set()  # Ключи модов, раскрытых/свёрнутых вручную вопреки умолчанию
        self.setDynamicSortFilter(False)

    def is_expanded(self, entry: Dict) -> bool:
        return (entry_key(entry) in self.toggled) != self.expand_all

    def set_expanded(self, entry: Dict, expanded: bool):
        key = entry_key(entry)
        if expanded != self.expand_all:
            self.toggled.add(key)
        else:
            self.toggled.discard(key)
        self.invalidateFilter()

    def set_expand_all(self, value: bool):
        self.expand_all = value
        self.toggled.clear()
        self.invalidateFilter()

    @property
    def filter_active(self) -> bool:
        return self.category != CAT_ALL or not self._query.empty or bool(self.tag)

    def _matches(self, entry: Optional[Dict]) -> bool:
        if not entry or not matches_category(entry, self.category):
            return False
        if self.tag and self.tag not in {key for key, _ in entry_tags(entry, self.custom_tags)}:
            return False
        return self._query.matches(entry, self.custom_tags)

    def set_tag(self, tag: Optional[str]):
        self.tag = tag or None
        self.invalidateFilter()

    # --- Группы ---

    def _clear_group_cache(self, *_):
        self._group_counts = None

    def group_counts(self) -> Dict[str, int]:
        """Число видимых модов (верхний уровень) в каждой группе."""
        if self._group_counts is None:
            counts: Dict[str, int] = {}
            for row in range(self.rowCount()):
                index = self.index(row, 0)
                if not meta_at(index).get("depth"):
                    key = group_of(entry_at(index))
                    counts[key] = counts.get(key, 0) + 1
            self._group_counts = counts
        return self._group_counts

    def header_for(self, row: int) -> Optional[str]:
        """Группа, заголовок которой рисуется над строкой (только при сортировке по важности)."""
        if self.sort_mode != self.SORT_STATUS:
            return None
        index = self.index(row, 0)
        if meta_at(index).get("depth"):
            return None
        group = group_of(entry_at(index))
        prev = row - 1
        while prev >= 0 and meta_at(self.index(prev, 0)).get("depth"):
            prev -= 1
        if prev >= 0 and group_of(entry_at(self.index(prev, 0))) == group:
            return None
        return group

    def _parent_visible(self, row: int, meta: Dict) -> bool:
        model = self.sourceModel()
        if self._matches(model.entry(row)):
            return True
        return self.filter_active and any(self._matches(model.entry(c)) for c in meta.get("children", ()))

    def set_filter(self, category: Optional[str] = None, query: Optional[str] = None):
        if category is not None:
            self.category = category
        if query is not None:
            self.query = query.strip()
            self._query = parse_query(self.query)
        self.invalidateFilter()

    def set_sort_mode(self, mode: str):
        self.sort_mode = mode
        self.invalidate()
        self.sort(0)

    def filterAcceptsRow(self, source_row, source_parent):
        model = self.sourceModel()
        meta = model.meta(source_row)
        if not meta.get("depth"):
            return self._parent_visible(source_row, meta)
        # Зависимость в дереве: видна под видимым раскрытым модом; при фильтре — ещё и если сама подходит под него
        parent_row = meta["parent_row"]
        if not self._parent_visible(parent_row, model.meta(parent_row)):
            return False
        return self.is_expanded(model.entry(parent_row)) or (self.filter_active and self._matches(model.entry(source_row)))

    def _sort_key(self, row: int) -> tuple:
        model = self.sourceModel()
        meta = model.meta(row)
        root = meta["parent_row"] if meta.get("depth") else row
        root_entry = model.entry(root)
        root_name = str(root_entry.get("name", "")).lower()
        favorite = 0 if root_entry.get("_favorite") else 1  # Избранные — наверху (в своей группе)
        if self.sort_mode == self.SORT_STATUS:
            root_key = (status_sort_key(root_entry), favorite, root_name)
        elif self.sort_mode == self.SORT_UPDATED:  # Сначала недавно обновлённые
            root_key = (favorite, -_timestamp(update_date(root_entry)), root_name)
        elif self.sort_mode == self.SORT_SIZE:  # Сначала самые большие
            root_key = (favorite, -(root_entry.get("size") or root_entry.get("new_size") or 0), root_name)
        else:
            root_key = (favorite, 0, root_name)
        # Зависимости идут сразу под своим модом, по имени
        return root_key + (root, meta.get("depth", 0), str(model.entry(row).get("name", "")).lower())

    def lessThan(self, left, right):
        return self._sort_key(left.row()) < self._sort_key(right.row())


class ModDelegate(QStyledItemDelegate):
    """Рисует строку мода: чекбокс, иконка, название + подпись, версии, статус, кнопки."""
    check_toggled = pyqtSignal(object)
    update_clicked = pyqtSignal(object)
    menu_requested = pyqtSignal(object, QPoint)
    expand_toggled = pyqtSignal(object)

    def __init__(self, view: QListView, tokens: dict, icon_provider: Callable[[Dict], Optional[QPixmap]]):
        super().__init__(view)
        self.view = view
        self.tokens = tokens
        self.icon_provider = icon_provider
        self._mouse = QPoint(-1, -1)
        self.compact = False   # Компактные строки: одна строка вместо двух
        self.tree = False      # Режим дерева: место под стрелку раскрытия
        self.groups = False    # Заголовки групп («Обновления», «Актуальные»…)
        self.columns = set(DEFAULT_COLUMNS)
        self.abandoned_hint = True  # Пометка «давно не обновлялся»
        self.is_expanded: Callable[[Dict], bool] = lambda entry: False
        self.anim: Dict[object, float] = {}  # Раскрытие дерева: ключ мода -> доля высоты его зависимостей (0..1)
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
        self.font_header = QFont(base)
        self.font_header.setPointSizeF(max(base.pointSizeF() - 1, 7.5))
        self.font_header.setWeight(QFont.Weight.Bold)
        self.font_header.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 106)

    def set_tokens(self, tokens: dict):
        self.tokens = tokens

    # --- Высота строки: заголовок группы и анимация раскрытия ---

    def _header(self, index) -> Optional[str]:
        proxy = index.model()
        if not self.groups or not isinstance(proxy, ModFilterProxy):
            return None
        return proxy.header_for(index.row())

    def _anim_factor(self, index) -> float:
        meta = meta_at(index)
        if not (self.tree and meta.get("depth") and self.anim):
            return 1.0
        source = index.model().sourceModel() if isinstance(index.model(), ModFilterProxy) else None
        parent = source.entry(meta["parent_row"]) if source else None
        return self.anim.get(entry_key(parent), 1.0) if parent else 1.0

    def _content_rect(self, rect: QRect, index) -> QRect:
        return rect.adjusted(0, HEADER_HEIGHT, 0, 0) if self._header(index) else rect

    def sizeHint(self, option, index):
        base = ROW_HEIGHT_COMPACT if self.compact else ROW_HEIGHT
        height = int(base * self._anim_factor(index))
        return QSize(option.rect.width(), height + (HEADER_HEIGHT if self._header(index) else 0))

    # --- Геометрия ---

    def _layout(self, rect: QRect, depth: int = 0) -> dict:
        r = rect.adjusted(14, 0, -10, 0)
        cy = r.center().y()
        left = r.left()
        lay = {}
        if self.tree:
            lay["expander"] = QRect(left - 6, cy - 12, 24, 24)
            left += 22 + depth * TREE_INDENT
        lay["check"] = QRect(left, cy - 9, 18, 18)
        icon_size = 28 if self.compact or depth else 36
        lay["icon"] = QRect(left + 32, cy - icon_size // 2, icon_size, icon_size)
        right = r.right()
        lay["more"] = QRect(right - 30, cy - 15, 30, 30)
        lay["update"] = QRect(lay["more"].left() - 34, cy - 15, 30, 30)
        # Узкий список (открыта панель мода, маленькое окно): статус — цветной точкой, подробности в подсказке
        lay["narrow"] = rect.width() < 560
        status_width = 18 if lay["narrow"] else 140
        lay["status"] = QRect(lay["update"].left() - status_width - 10, cy - 12, status_width, 24)
        # Необязательные колонки: «Размер / дата / сторона» и «Версии» — только если хватает ширины.
        # Широкий экран: каждое значение в своей колонке; иначе — стопкой в одной узкой колонке
        meta_keys = [key for key in META_KEYS if key in self.columns]
        meta_on = bool(meta_keys) and rect.width() > 640
        spread = meta_on and rect.width() > 1000 + 90 * len(meta_keys)
        stack_width = 170 if self.compact and len(meta_keys) > 1 and rect.width() > 900 else 120  # Компактно — в строку
        meta_width = (sum(META_WIDTHS[k] for k in meta_keys) if spread else stack_width) if meta_on else 0
        lay["meta"] = QRect(lay["status"].left() - meta_width - 10, r.top(), meta_width, r.height())
        lay["meta_spread"] = meta_keys if spread else None
        wide = rect.width() > 820 and "versions" in self.columns
        version_width = ((230 if meta_on else 250) + min(130, max(0, rect.width() - 1300) // 4)) if wide else 0
        lay["versions"] = QRect(lay["meta"].left() - version_width - (10 if meta_on else 0), r.top(), version_width,
                                r.height())
        lay["meta_on"] = meta_on
        text_left = lay["icon"].right() + 12
        text_right = (lay["versions"].left() if wide else lay["meta"].left() if meta_on else lay["status"].left()) - 14
        lay["text"] = QRect(text_left, r.top(), max(40, text_right - text_left), r.height())
        lay["wide"] = wide
        return lay

    # --- Отрисовка ---

    def paint(self, painter: QPainter, option, index):
        entry = entry_at(index)
        if not entry:
            return
        t = self.tokens
        meta = meta_at(index)
        depth = meta.get("depth", 0) if self.tree else 0
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        header = self._header(index)
        if header:
            self._paint_header(painter, option.rect, header, index)
        rect = self._content_rect(option.rect, index)
        factor = self._anim_factor(index)
        if factor < 1.0:  # Строка раскрывается: проявляется вместе с ростом высоты
            painter.setClipRect(option.rect)
            painter.setOpacity(factor)
            rect = QRect(rect.left(), rect.top() - int((1 - factor) * rect.height()), rect.width(),
                         ROW_HEIGHT_COMPACT if self.compact else ROW_HEIGHT)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver) and rect.contains(self._mouse)
        lay = self._layout(rect, depth)

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

        if self.tree:
            # Ветка дерева: вертикаль от стрелки мода через все зависимости, к каждой — отвод
            painter.setPen(QPen(qcolor(t["border_strong"]), 1.2))
            x = lay["expander"].center().x() + 1
            cy = rect.center().y()
            if meta.get("children"):
                expanded = self.is_expanded(entry)
                self._paint_expander(painter, lay["expander"], expanded, lay["expander"].contains(self._mouse) and hovered)
                if expanded:
                    painter.setPen(QPen(qcolor(t["border_strong"]), 1.2))
                    painter.drawLine(x, lay["expander"].bottom() + 2, x, rect.bottom() + 1)
            elif depth:
                painter.drawLine(x, rect.top(), x, cy if meta.get("last") else rect.bottom() + 1)
                painter.drawLine(x, cy, lay["check"].left() - 8, cy)
        self._paint_checkbox(painter, lay["check"], bool(entry.get("_is_checked")), hovered)
        base_opacity = painter.opacity()
        if entry.get("disabled"):  # Выключенный мод — приглушённым
            painter.setOpacity(base_opacity * 0.45)
        self._paint_icon(painter, lay["icon"], entry)
        self._paint_texts(painter, lay, entry, meta.get("dep_type") if depth else None)
        if lay["wide"]:
            self._paint_versions(painter, lay["versions"], entry)
        if lay["meta_on"]:
            self._paint_meta(painter, lay["meta"], entry, lay["meta_spread"])
        painter.setOpacity(base_opacity)
        self._paint_status(painter, lay["status"], entry)

        status = status_of(entry)
        busy = status in (ModStatus.DOWNLOADING, ModStatus.FINISHING)
        if entry.get("download_url") and not busy and not entry.get("_pinned"):
            self._paint_round_button(painter, lay["update"], "download", accent=True,
                                     hot=lay["update"].contains(self._mouse) and hovered)
        if hovered or selected:
            self._paint_round_button(painter, lay["more"], "more", accent=False,
                                     hot=lay["more"].contains(self._mouse) and hovered)
        painter.restore()

    def _paint_header(self, p: QPainter, rect: QRect, group: str, index):
        t = self.tokens
        r = QRect(rect.left() + 18, rect.top() + 10, rect.width() - 36, HEADER_HEIGHT - 12)
        title = GROUP_TITLES.get(group, group).upper()
        count = index.model().group_counts().get(group, 0) if isinstance(index.model(), ModFilterProxy) else 0
        p.setFont(self.font_header)
        fm = QFontMetrics(self.font_header)
        color = {"problems": "warning", "updates": "accent", "install": "info"}.get(group, "text_faint")
        p.setPen(qcolor(t[color]))
        p.drawText(r, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        x = r.left() + fm.horizontalAdvance(title) + 8
        p.setPen(qcolor(t["text_faint"]))
        p.drawText(QRect(x, r.top(), 60, r.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, str(count))
        line_x = x + fm.horizontalAdvance(str(count)) + 12
        p.setPen(QPen(qcolor(t["border"]), 1))
        p.drawLine(line_x, r.center().y() + 1, r.right(), r.center().y() + 1)

    def _paint_expander(self, p: QPainter, r: QRect, expanded: bool, hot: bool):
        t = self.tokens
        if hot:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(t["surface3"]))
            p.drawRoundedRect(QRectF(r), 6, 6)
        p.drawPixmap(r.adjusted(4, 4, -4, -4), icons.pixmap("chevron_down" if expanded else "chevron_right",
                                                           t["text"] if hot else t["text_muted"], 16))

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

    def _paint_texts(self, p: QPainter, lay: dict, entry: Dict, dep_type: Optional[str] = None):
        t = self.tokens
        r = lay["text"]
        name = str(entry.get("name") or "")
        p.setFont(self.font_name)
        p.setPen(qcolor(t["text"]))
        fm = QFontMetrics(self.font_name)
        if self.compact:
            self._paint_compact_texts(p, lay, entry, dep_type, name, fm)
            return
        name_rect = QRect(r.left(), r.top() + 11, r.width(), fm.height())
        self._paint_name_line(p, name_rect, entry, name, fm)

        subtitle, role = self._subtitle(entry, dep_type)
        if not lay["wide"] and entry.get("current_version"):
            subtitle = self._versions_text(entry) + "  ·  " + subtitle
        p.setFont(self.font_small)
        p.setPen(qcolor(t[role] if role else t["text_muted"]))
        fm_small = QFontMetrics(self.font_small)
        sub_rect = QRect(r.left(), name_rect.bottom() + 3, r.width(), fm_small.height())
        p.drawText(sub_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   fm_small.elidedText(subtitle, Qt.TextElideMode.ElideRight, r.width()))

    def _badges(self, entry: Dict) -> List[tuple]:
        """Значки после названия: (иконка или None, текст, цвет)."""
        t = self.tokens
        badges = []
        if entry.get("_pinned"):
            badges.append(("lock", "", t["text_muted"]))
        if entry.get("_note"):
            badges.append(("pencil", "", t["info"]))
        if entry.get("_new"):
            badges.append((None, tr("новое"), t["accent"]))
        if self.abandoned_hint and entry.get("filepath") and is_abandoned(entry):
            badges.append((None, tr("давно не обновлялся"), t["warning"]))
        return badges

    def _paint_name_line(self, p: QPainter, rect: QRect, entry: Dict, name: str, fm: QFontMetrics) -> int:
        """Звезда избранного, название и значки. Возвращает ширину занятой части."""
        t = self.tokens
        x = rect.left()
        if entry.get("_favorite"):
            p.drawPixmap(QRect(x, rect.center().y() - 7, 14, 14), icons.pixmap("star_filled", t["warning"], 14))
            x += 19
        fm_chip = QFontMetrics(self.font_chip)
        badges = self._badges(entry)
        badges_width = sum((16 if icon else fm_chip.horizontalAdvance(text) + 14) + 6 for icon, text, _ in badges)
        available = max(30, rect.right() - x - badges_width)
        elided = fm.elidedText(name, Qt.TextElideMode.ElideRight, available)
        p.setFont(self.font_name)
        p.setPen(qcolor(t["text"]))
        p.drawText(QRect(x, rect.top(), available, rect.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   elided)
        x += fm.horizontalAdvance(elided) + 7
        for icon_name, text, color in badges:
            if icon_name:
                p.drawPixmap(QRect(x, rect.center().y() - 6, 13, 13), icons.pixmap(icon_name, color, 13))
                x += 19
            else:
                w = fm_chip.horizontalAdvance(text) + 14
                chip = QRectF(x, rect.center().y() - 8, w, 16)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(qcolor(color, 0.14))
                p.drawRoundedRect(chip, 8, 8)
                p.setFont(self.font_chip)
                p.setPen(qcolor(color))
                p.drawText(chip, Qt.AlignmentFlag.AlignCenter, text)
                x += w + 6
        return x - rect.left()

    def _subtitle(self, entry: Dict, dep_type: Optional[str], fallback: bool = True) -> tuple:
        """Вторая строка: проблемы важнее всего, затем заметка, затем выбранные колонки (описание, метки)."""
        text, role = subtitle_for(entry)
        if role:  # Предупреждение (проблема, «нужен для…»)
            return text, role
        if entry.get("_note"):
            return "✎ " + " ".join(str(entry["_note"]).split()), "info"
        parts = []
        if dep_type:
            kind = DEP_TYPE_LABELS.get(dep_type, "")
            parts.append(tr('{0}{1} зависимость', kind[:1].upper(), kind[1:]))
        if "description" in self.columns and entry.get("description"):
            parts.append(" ".join(entry["description"].split()))
        if "tags" in self.columns:
            tags = [title for key, title in entry_tags(entry) if not key.startswith("side:")]
            if tags:
                parts.append("# " + ", ".join(tags[:4]))
        if not parts and fallback:
            path = entry.get("filepath") or entry.get("new_filename") or ""
            parts.append(path.replace("\\", "/").rsplit("/", 1)[-1])
        return "  ·  ".join(parts), None

    def _meta_values(self, entry: Dict) -> Dict[str, str]:
        values = {}
        if "size" in self.columns and (entry.get("size") or entry.get("new_size")):
            values["size"] = format_size(entry.get("size") or entry.get("new_size"))
        if "date" in self.columns:
            values["date"] = format_date(update_date(entry)) or ""
        if "side" in self.columns:
            side = side_tag(entry)
            values["side"] = SIDE_SHORT.get(side[0], side[1]) if side else ""
        return values

    def _paint_meta(self, p: QPainter, r: QRect, entry: Dict, spread: Optional[List[str]] = None):
        """Размер / дата / сторона: отдельными колонками (широкий экран) или стопкой в одной колонке."""
        t = self.tokens
        values = self._meta_values(entry)
        p.setFont(self.font_small)
        p.setPen(qcolor(t["text_muted"]))
        fm = QFontMetrics(self.font_small)
        if spread:
            x = r.left()
            for key in spread:
                cell = QRect(x, r.top(), META_WIDTHS[key] - 10, r.height())
                p.drawText(cell, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                           fm.elidedText(values.get(key, ""), Qt.TextElideMode.ElideRight, cell.width()))
                x += META_WIDTHS[key]
            return
        lines = [v for v in values.values() if v]
        if not lines:
            return
        if self.compact:  # Одна строка — через точку, только значения, которые помещаются целиком
            joined = lines[0]
            for value in lines[1:]:
                if fm.horizontalAdvance(f"{joined}  ·  {value}") > r.width():
                    break
                joined = f"{joined}  ·  {value}"
            lines = [joined]
        else:
            lines = lines[:3] if len(lines) == 3 and r.height() >= 3 * fm.height() + 8 else lines[:2]
        top = r.center().y() - (fm.height() * len(lines) + 2 * (len(lines) - 1)) // 2
        for i, line in enumerate(lines):
            p.drawText(QRect(r.left(), top + i * (fm.height() + 2), r.width(), fm.height()),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       fm.elidedText(line, Qt.TextElideMode.ElideRight, r.width()))

    def _paint_compact_texts(self, p: QPainter, lay: dict, entry: Dict, dep_type: Optional[str], name: str,
                             fm: QFontMetrics):
        """Одна строка: название и короткая пометка рядом (тип зависимости, проблема, версии в узком окне)."""
        t = self.tokens
        r = lay["text"]
        name_w = min(self._paint_name_line(p, QRect(r.left(), r.top(), r.width() * 2 // 3, r.height()), entry, name, fm),
                     r.width())
        note, role = self._subtitle(entry, dep_type, fallback=False)
        if entry.get("_issues"):
            note, role = "⚠ " + entry["_issues"][0], "warning"
        if not lay["wide"] and entry.get("current_version"):
            note = self._versions_text(entry) + (f"  ·  {note}" if note else "")
        if note and r.width() - name_w > 40:
            p.setFont(self.font_small)
            p.setPen(qcolor(t[role] if role else t["text_faint"]))
            note_rect = QRect(r.left() + name_w + 10, r.top(), r.width() - name_w - 10, r.height())
            p.drawText(note_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       QFontMetrics(self.font_small).elidedText(note, Qt.TextElideMode.ElideRight, note_rect.width()))

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
        if status == ModStatus.DOWNLOADING and r.width() < 40:  # Узкий режим: только процент
            p.setFont(self.font_chip)
            p.setPen(qcolor(t["accent"]))
            text = f"{int(progress * 100)}%" if progress is not None else "…"
            p.drawText(QRect(r.right() - 44, r.top(), 46, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, text)
            return
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
                label = tr("Скачивание…")
            p.setFont(self.font_chip)
            p.setPen(qcolor(t["text_muted"]))
            p.drawText(QRect(r.left(), r.top() - 12, r.width(), 14), Qt.AlignmentFlag.AlignRight, label)
            return

        label, role = status_style(entry)
        color = t.get(role, t["text_muted"])
        if r.width() < 40:  # Узкий режим: только точка
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(color))
            p.drawEllipse(QRectF(r.center().x() - 4, r.center().y() - 4, 8, 8))
            return
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
        meta = meta_at(index)
        lay = self._layout(self._content_rect(option.rect, index), meta.get("depth", 0) if self.tree else 0)
        if self.tree and meta.get("children") and lay["expander"].contains(pos):
            if etype == QEvent.Type.MouseButtonRelease:
                self.expand_toggled.emit(entry)
            return True
        hit_check = lay["check"].adjusted(-8, -12, 8, 12).contains(pos)
        hit_update = bool(entry.get("download_url")) and not entry.get("_pinned") and lay["update"].contains(pos)
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
        meta = meta_at(index)
        lay = self._layout(self._content_rect(option.rect, index), meta.get("depth", 0) if self.tree else 0)
        tip = None
        if self.tree and meta.get("children") and lay["expander"].contains(pos):
            tip = tr('Зависимости: {0}', len(meta['children']))
        elif entry.get("download_url") and not entry.get("_pinned") and lay["update"].contains(pos):
            verb = tr("Установить") if not entry.get("filepath") else tr("Обновить до")
            tip = f"{verb} {entry.get('new_version') or ''}".strip()
        elif lay["more"].contains(pos):
            tip = tr("Действия")
        elif lay["check"].adjusted(-8, -12, 8, 12).contains(pos):
            tip = tr("Отметить для массовых действий")
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
    expand_requested = pyqtSignal(bool)   # → раскрыть, ← свернуть (режим дерева)
    open_requested = pyqtSignal()         # Enter — открыть панель мода

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ModList")
        self.setMouseTracking(True)
        self.setUniformItemSizes(False)  # Высота строк разная: заголовки групп и анимация раскрытия
        self.setMinimumWidth(280)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(18)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSpacing(0)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def relayout(self):
        """Пересчитать высоты строк (после смены вида или кадра анимации)."""
        self.scheduleDelayedItemsLayout()

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
        if event.key() in (Qt.Key.Key_Right, Qt.Key.Key_Left):
            self.expand_requested.emit(event.key() == Qt.Key.Key_Right)
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.open_requested.emit()
            return
        super().keyPressEvent(event)

    def leaveEvent(self, event):
        delegate = self.itemDelegate()
        if isinstance(delegate, ModDelegate):
            delegate._mouse = QPoint(-1, -1)
        self.viewport().update()
        super().leaveEvent(event)

