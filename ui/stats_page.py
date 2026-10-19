"""
Страница «Статистика»: плитки с главными числами и горизонтальные столбцы (одна величина — один цвет).
Подписи — цветами текста, столбцы — акцентом темы; при наведении подсказка, щелчок фильтрует список модов.
"""
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFontMetrics, QPainter
from PyQt6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QToolTip, QVBoxLayout, QWidget,
)

from stats import build_stats
from theme import qcolor
from ui.widgets import EmptyState, button, label, refresh_button_icons
from view_model import format_size

BAR_HEIGHT = 10
ROW_HEIGHT = 30


class BarChart(QWidget):
    """Горизонтальные столбцы: подпись слева, столбец, значение справа. item = (подпись, величина, текст значения, данные)."""
    item_clicked = pyqtSignal(object)

    def __init__(self, tokens: dict, parent=None):
        super().__init__(parent)
        self.tokens = tokens
        self.items: List[tuple] = []
        self._hover = -1
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_items(self, items: List[tuple]):
        self.items = items
        self.setFixedHeight(max(ROW_HEIGHT, len(items) * ROW_HEIGHT))
        self.update()

    def sizeHint(self):
        return QSize(400, max(ROW_HEIGHT, len(self.items) * ROW_HEIGHT))

    def _geometry(self):
        fm = QFontMetrics(self.font())
        label_w = min(230, max([fm.horizontalAdvance(i[0]) for i in self.items] + [60]) + 24)
        value_w = max([fm.horizontalAdvance(i[2]) for i in self.items] + [40]) + 12
        return label_w, value_w

    def paintEvent(self, event):
        if not self.items:
            return
        t = self.tokens
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        fm = QFontMetrics(self.font())
        label_w, value_w = self._geometry()
        track_w = max(40, self.width() - label_w - value_w)
        top_value = max(i[1] for i in self.items) or 1
        for row, (title, value, text, _) in enumerate(self.items):
            y = row * ROW_HEIGHT
            hot = row == self._hover
            if hot:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(qcolor(t["surface2"]))
                p.drawRoundedRect(QRectF(0, y + 1, self.width(), ROW_HEIGHT - 2), 6, 6)
            p.setPen(qcolor(t["text"] if hot else t["text_muted"]))
            p.drawText(QRect(6, y, label_w - 14, ROW_HEIGHT), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       fm.elidedText(title, Qt.TextElideMode.ElideRight, label_w - 14))
            width = max(4.0, track_w * value / top_value)
            bar = QRectF(label_w, y + (ROW_HEIGHT - BAR_HEIGHT) / 2, width, BAR_HEIGHT)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(t["accent"], 1.0 if hot or self._hover < 0 else 0.55))
            p.drawRoundedRect(bar, 4, 4)
            p.setPen(qcolor(t["text"]))
            p.drawText(QRect(int(bar.right()) + 8, y, value_w, ROW_HEIGHT),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        p.end()

    def mouseMoveEvent(self, event):
        row = int(event.position().y() // ROW_HEIGHT)
        row = row if 0 <= row < len(self.items) else -1
        if row != self._hover:
            self._hover = row
            self.update()
        if row >= 0:
            title, _, text, _ = self.items[row]
            QToolTip.showText(event.globalPosition().toPoint(), f"<b>{title}</b><br>{text}", self)
        self.setCursor(Qt.CursorShape.PointingHandCursor if row >= 0 else Qt.CursorShape.ArrowCursor)

    def leaveEvent(self, event):
        self._hover = -1
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and 0 <= self._hover < len(self.items):
            self.item_clicked.emit(self.items[self._hover][3])


class StatTile(QFrame):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(2)
        self.value = QLabel("—")
        self.value.setObjectName("StatValue")
        self.caption = label(title, "Muted")
        layout.addWidget(self.value)
        layout.addWidget(self.caption)

    def set_value(self, text: str, caption: Optional[str] = None):
        self.value.setText(text)
        if caption:
            self.caption.setText(caption)


class StatsPage(QWidget):
    """Сигналы: filter_requested(строка поиска) — показать моды в списке; entry_requested(запись)."""
    filter_requested = pyqtSignal(str)
    entry_requested = pyqtSignal(object)
    replacement_requested = pyqtSignal(object)

    def __init__(self, tokens: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.tokens = tokens
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        root = QVBoxLayout(content)
        root.setContentsMargins(32, 28, 32, 24)
        root.setSpacing(16)
        head = QVBoxLayout()
        head.setSpacing(4)
        head.addWidget(label("Статистика", "PageTitle"))
        self.subtitle = label("", "PageSubtitle", wrap=True)
        head.addWidget(self.subtitle)
        root.addLayout(head)

        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.tiles: Dict[str, StatTile] = {}
        for key, title in (("count", "модов в сборке"), ("size", "занимают на диске"), ("update", "скачается при обновлении"),
                           ("disabled", "выключены"), ("abandoned", "давно не обновлялись")):
            tile = StatTile(title)
            self.tiles[key] = tile
            tiles.addWidget(tile)
        root.addLayout(tiles)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)
        self.categories = BarChart(tokens)
        self.categories.item_clicked.connect(lambda title: self.filter_requested.emit(f'tag:"{title}"'))
        self.heaviest = BarChart(tokens)
        self.heaviest.item_clicked.connect(self.entry_requested)
        self.sides = BarChart(tokens)
        self.sides.item_clicked.connect(lambda query: query and self.filter_requested.emit(query))
        grid.addWidget(self._section("Размер по категориям", "Щёлкните по строке, чтобы увидеть эти моды в списке.",
                                     self.categories), 0, 0)
        grid.addWidget(self._section("Самые тяжёлые моды", "Размер файла.", self.heaviest), 0, 1)
        grid.addWidget(self._section("Где нужен мод", "Сторона по данным Modrinth: клиент, сервер или оба.",
                                     self.sides), 1, 0)
        self.abandoned_box = QVBoxLayout()
        self.abandoned_box.setSpacing(6)
        abandoned_widget = QWidget()
        abandoned_widget.setLayout(self.abandoned_box)
        self.abandoned_box.setContentsMargins(0, 0, 0, 0)
        grid.addWidget(self._section("Давно не обновлялись", "Автор не выпускал версий больше года — "
                                     "такие моды стоит заменить живыми аналогами.", abandoned_widget), 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self.grid_widget = QWidget()
        self.grid_widget.setLayout(grid)
        grid.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.grid_widget)
        self.empty = EmptyState()
        root.addWidget(self.empty)
        root.addStretch()

    def _section(self, title: str, hint: str, widget: QWidget) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(6)
        layout.addWidget(label(title, "SectionTitle"))
        layout.addWidget(label(hint, "Faint", wrap=True))
        layout.addSpacing(4)
        layout.addWidget(widget)
        layout.addStretch()
        return card

    def set_tokens(self, tokens: dict):
        self.tokens = tokens
        for chart in (self.categories, self.heaviest, self.sides):
            chart.tokens = tokens
            chart.update()
        refresh_button_icons(self, tokens)

    def set_data(self, entries: List[Dict], profile_name: str, custom_tags: Optional[Dict] = None,
                 icon_provider: Optional[Callable] = None):
        stats = build_stats(entries, custom_tags)
        has_data = stats["count"] > 0
        self.subtitle.setText(f"Сборка «{profile_name}». Данные о категориях, стороне и датах — с Modrinth "
                              "(появляются после проверки обновлений).")
        self.tiles["count"].set_value(str(stats["count"]), f"модов в сборке · {stats['libraries']} библиотек")
        self.tiles["size"].set_value(format_size(stats["total_size"]))
        self.tiles["update"].set_value(format_size(stats["update_size"]) if stats["update_size"] else "0")
        self.tiles["disabled"].set_value(str(stats["disabled"]))
        self.tiles["abandoned"].set_value(str(len(stats["abandoned"])))
        self.categories.set_items([(title, size, f"{format_size(size)} · {count}", title)
                                   for title, count, size in stats["by_category"][:10]])
        self.heaviest.set_items([(e.get("name") or "?", e.get("size") or 0, format_size(e.get("size") or 0), e)
                                 for e in stats["heaviest"]])
        side_queries = {"Только клиент": "side:client", "Только сервер": "side:server", "Клиент и сервер": "side:both",
                        "Клиент или сервер": "side:any"}
        self.sides.set_items([(title, n, str(n), side_queries.get(title, "")) for title, n in stats["sides"]])
        while self.abandoned_box.count():
            item = self.abandoned_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not stats["abandoned"]:
            self.abandoned_box.addWidget(label("Таких модов нет — все обновлялись в последний год.", "Muted", wrap=True))
        for entry, days in stats["abandoned"][:8]:
            row = QHBoxLayout()
            name = label(f"{entry.get('name')}   <span style='color:{self.tokens['text_faint']}'>"
                         f"{days // 365} г. {days % 365 // 30} мес. назад</span>")
            name.setTextFormat(Qt.TextFormat.RichText)
            row.addWidget(name, 1)
            find = button("Замена", "GhostButton", "compass", self.tokens, "text", "Найти похожие моды в каталоге")
            find.clicked.connect(lambda _=False, e=entry: self.replacement_requested.emit(e))
            row.addWidget(find)
            holder = QWidget()
            holder.setLayout(row)
            row.setContentsMargins(0, 0, 0, 0)
            self.abandoned_box.addWidget(holder)
        for widget in (*self.tiles.values(), self.grid_widget):
            widget.setVisible(has_data)
        self.empty.setVisible(not has_data)
        if not has_data:
            self.empty.set_content(self.tokens, "bar_chart", "Пока нечего считать",
                                   "Откройте сборку с модами — здесь появится её статистика.")
