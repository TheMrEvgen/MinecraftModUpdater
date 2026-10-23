"""
Страница «Статистика»: плитки с главными числами и горизонтальные столбцы (одна величина — один цвет).
Подписи — цветами текста, столбцы — акцентом темы; при наведении подсказка, щелчок фильтрует список модов.
"""
import datetime
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
from i18n import tr

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

    def minimumSizeHint(self):
        return QSize(220, max(ROW_HEIGHT, len(self.items) * ROW_HEIGHT))

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


class LaunchChart(QWidget):
    """Последние запуски игры: высота столбца — время загрузки, вылеты — красным во всю высоту."""
    HEIGHT = 120
    MAX_RUNS = 25

    def __init__(self, tokens: dict, parent=None):
        super().__init__(parent)
        self.tokens = tokens
        self.runs: List[Dict] = []
        self._hover = -1
        self.setMouseTracking(True)
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_runs(self, runs: List[Dict]):
        self.runs = list(runs or [])[-self.MAX_RUNS:]
        self._hover = -1
        self.update()

    def _slot(self) -> float:
        return self.width() / max(len(self.runs), 8)

    def paintEvent(self, event):
        if not self.runs:
            return
        t = self.tokens
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        slot = self._slot()
        bar_w = max(4.0, min(22.0, slot * 0.6))
        top = max([r.get("load") or 0 for r in self.runs] + [1])
        for i, run in enumerate(self.runs):
            x = i * slot + (slot - bar_w) / 2
            if run.get("crashed"):
                height, color = self.HEIGHT - 6, qcolor(t["danger"], 0.85)
            elif run.get("load"):
                height, color = max(6.0, (self.HEIGHT - 6) * run["load"] / top), qcolor(t["accent"])
            else:  # Закрыли до меню — время неизвестно
                height, color = 6.0, qcolor(t["text_faint"])
            if self._hover >= 0 and i != self._hover:
                color.setAlphaF(color.alphaF() * 0.5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(QRectF(x, self.HEIGHT - height, bar_w, height), 3, 3)
        p.end()

    def mouseMoveEvent(self, event):
        index = int(event.position().x() // self._slot())
        index = index if 0 <= index < len(self.runs) else -1
        if index != self._hover:
            self._hover = index
            self.update()
        if index >= 0:
            run = self.runs[index]
            when = datetime.datetime.fromtimestamp(run["start"]).strftime("%d.%m %H:%M")
            if run.get("crashed"):
                what = tr("вылет")
            elif run.get("load"):
                what = tr("загрузка {0} с", f"{run['load']:.0f}")
            else:
                what = tr("закрыта до главного меню")
            QToolTip.showText(event.globalPosition().toPoint(), f"<b>{when}</b><br>{what}", self)

    def leaveEvent(self, event):
        self._hover = -1
        self.update()
        super().leaveEvent(event)


class StatTile(QFrame):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(2)
        self.value = QLabel("—")
        self.value.setObjectName("StatValue")
        self.caption = label(title, "Muted", wrap=True)
        self.setMinimumWidth(150)
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
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        root = QVBoxLayout(content)
        root.setContentsMargins(32, 28, 32, 24)
        root.setSpacing(16)
        head = QVBoxLayout()
        head.setSpacing(4)
        head.addWidget(label(tr("Статистика"), "PageTitle"))
        self.subtitle = label("", "PageSubtitle", wrap=True)
        head.addWidget(self.subtitle)
        root.addLayout(head)

        self.tiles_grid = QGridLayout()
        self.tiles_grid.setSpacing(10)
        self.tiles: Dict[str, StatTile] = {}
        for key, title in (("count", tr("модов в сборке")), ("size", tr("занимают на диске")), ("update", tr("скачается при обновлении")),
                           ("disabled", tr("выключены")), ("abandoned", tr("давно не обновлялись"))):
            self.tiles[key] = StatTile(title)
        root.addLayout(self.tiles_grid)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)
        self.charts_grid = grid
        self.categories = BarChart(tokens)
        self.categories.item_clicked.connect(lambda title: self.filter_requested.emit(f'tag:"{title}"'))
        self.heaviest = BarChart(tokens)
        self.heaviest.item_clicked.connect(self.entry_requested)
        self.sides = BarChart(tokens)
        self.sides.item_clicked.connect(lambda query: query and self.filter_requested.emit(query))
        self.sections = [
            self._section(tr("Размер по категориям"), tr("Щёлкните по строке, чтобы увидеть эти моды в списке."), self.categories),
            self._section(tr("Самые тяжёлые моды"), tr("Размер файла."), self.heaviest),
            self._section(tr("Где нужен мод"), tr("Сторона по данным Modrinth: клиент, сервер или оба."), self.sides),
        ]
        self.abandoned_box = QVBoxLayout()
        self.abandoned_box.setSpacing(6)
        abandoned_widget = QWidget()
        abandoned_widget.setLayout(self.abandoned_box)
        self.abandoned_box.setContentsMargins(0, 0, 0, 0)
        self.sections.append(self._section(tr("Давно не обновлялись"), tr("Автор не выпускал версий больше года — "
                                           "такие моды стоит заменить живыми аналогами."), abandoned_widget))
        launches = QWidget()
        launches_layout = QVBoxLayout(launches)
        launches_layout.setContentsMargins(0, 0, 0, 0)
        launches_layout.setSpacing(8)
        self.launch_summary = label("", None, wrap=True)
        self.launch_chart = LaunchChart(tokens)
        launches_layout.addWidget(self.launch_summary)
        launches_layout.addWidget(self.launch_chart)
        self.sections.append(self._section(tr("Запуски игры"), tr("Время загрузки до главного меню по логу игры, "
                                           "красным — вылеты. Учитываются запуски, пока эта сборка открыта в программе."),
                                           launches))
        self._columns = None
        self._arrange(2)
        self.grid_widget = QWidget()
        self.grid_widget.setLayout(grid)
        grid.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.grid_widget)
        self.empty = EmptyState()
        root.addWidget(self.empty)
        root.addStretch()

    def _arrange(self, columns: int):
        """Плитки и графики по ширине страницы: в узком окне — в один-два столбца."""
        if columns == self._columns:
            return
        self._columns = columns
        tile_columns = 5 if columns == 2 else 3
        for i, tile in enumerate(self.tiles.values()):
            self.tiles_grid.addWidget(tile, i // tile_columns, i % tile_columns)
        for i, section in enumerate(self.sections):
            self.charts_grid.addWidget(section, i // columns, i % columns)
        for c in range(2):
            self.charts_grid.setColumnStretch(c, 1 if c < columns else 0)
        for c in range(5):
            self.tiles_grid.setColumnStretch(c, 1 if c < tile_columns else 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange(2 if self.width() >= 960 else 1)

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
        for chart in (self.categories, self.heaviest, self.sides, self.launch_chart):
            chart.tokens = tokens
            chart.update()
        refresh_button_icons(self, tokens)

    def set_launches(self, runs: Optional[List[Dict]]):
        import launch_history
        stats = launch_history.stats(runs)
        self.launch_chart.set_runs(runs or [])
        self.launch_chart.setVisible(bool(runs))
        if not stats["runs"]:
            self.launch_summary.setText(tr("Запусков пока не было — нажмите «Играть» или запустите сборку в лаунчере."))
            return
        parts = [tr("Запусков: {0}", stats["runs"]), tr("вылетов: {0}", stats["crashes"])]
        if stats["average_load"]:
            parts.append(tr("загрузка в среднем {0} с (последняя {1} с)", f"{stats['average_load']:.0f}",
                            f"{stats['last_load']:.0f}"))
        self.launch_summary.setText(" · ".join(parts))

    def set_data(self, entries: List[Dict], profile_name: str, custom_tags: Optional[Dict] = None,
                 icon_provider: Optional[Callable] = None):
        stats = build_stats(entries, custom_tags)
        has_data = stats["count"] > 0
        self.subtitle.setText(tr('Сборка «{0}». Данные о категориях, стороне и датах — с Modrinth (появляются после проверки обновлений).', profile_name))
        self.tiles["count"].set_value(str(stats["count"]), tr('модов в сборке · {0} библиотек', stats['libraries']))
        self.tiles["size"].set_value(format_size(stats["total_size"]))
        self.tiles["update"].set_value(format_size(stats["update_size"]) if stats["update_size"] else "0")
        self.tiles["disabled"].set_value(str(stats["disabled"]))
        self.tiles["abandoned"].set_value(str(len(stats["abandoned"])))
        self.categories.set_items([(title, size, f"{format_size(size)} · {count}", title)
                                   for title, count, size in stats["by_category"][:10]])
        self.heaviest.set_items([(e.get("name") or "?", e.get("size") or 0, format_size(e.get("size") or 0), e)
                                 for e in stats["heaviest"]])
        side_queries = {tr("Только клиент"): "side:client", tr("Только сервер"): "side:server", tr("Клиент и сервер"): "side:both",
                        tr("Клиент или сервер"): "side:any"}
        self.sides.set_items([(title, n, str(n), side_queries.get(title, "")) for title, n in stats["sides"]])
        while self.abandoned_box.count():
            item = self.abandoned_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not stats["abandoned"]:
            self.abandoned_box.addWidget(label(tr("Таких модов нет — все обновлялись в последний год."), "Muted", wrap=True))
        for entry, days in stats["abandoned"][:8]:
            row = QHBoxLayout()
            name = label(tr("{0}   <span style='color:{1}'>{2} г. {3} мес. назад</span>", entry.get('name'), self.tokens['text_faint'], days // 365, days % 365 // 30))
            name.setTextFormat(Qt.TextFormat.RichText)
            row.addWidget(name, 1)
            find = button(tr("Замена"), "GhostButton", "compass", self.tokens, "text", tr("Найти похожие моды в каталоге"))
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
            self.empty.set_content(self.tokens, "bar_chart", tr("Пока нечего считать"),
                                   tr("Откройте сборку с модами — здесь появится её статистика."))
