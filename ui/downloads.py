"""
Загрузки как в Steam: индикатор внизу боковой панели (виден на любой странице) и страница «Загрузки» —
общий прогресс, скорость с графиком, сколько осталось и каждый файл со своим прогрессом.
Данные — downloads.MANAGER; окно несколько раз в секунду вызывает refresh().
"""
import time
from typing import List, Optional

from PyQt6.QtCore import QAbstractListModel, QModelIndex, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFontMetrics, QPainter, QPainterPath
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QListView, QListWidget, QListWidgetItem, QProgressBar, QSizePolicy, QStyledItemDelegate,
    QVBoxLayout, QWidget,
)

import downloads
import icons
from i18n import tr
from theme import qcolor
from ui.widgets import EmptyState, button, label, refresh_button_icons
from view_model import format_size

DONE_VISIBLE_SECONDS = 20  # Сколько индикатор показывает «Готово» после завершения


def job_status_line(job: downloads.DownloadJob) -> str:
    totals = job.totals()
    if job.finished is not None:
        return job.summary or {downloads.DONE: tr("Готово"), downloads.FAILED: tr("С ошибками"),
                               downloads.CANCELLED: tr("Отменено")}.get(job.state, "")
    size = format_size(totals["size"]) if totals["size"] else "?"
    special = downloads.status_text(job)
    if special:
        return tr("{0} из {1} · {2}", format_size(totals["bytes"]), size, special)
    return tr("{0} из {1} · {2}/с · осталось {3}", format_size(totals["bytes"]), size, format_size(job.speed()),
              downloads.format_eta(job.eta()))


# --- Индикатор в боковой панели ---

class DownloadIndicator(QWidget):
    """Карточка внизу боковой панели: что качается, процент, скорость и время. Щелчок — страница «Загрузки»."""
    clicked = pyqtSignal()

    def __init__(self, tokens: dict, parent=None):
        super().__init__(parent)
        self.tokens = tokens
        self.collapsed = False
        self.job: Optional[downloads.DownloadJob] = None
        self.count = 0
        self._hover = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(58)
        self.hide()

    def set_collapsed(self, collapsed: bool):
        self.collapsed = collapsed
        self.setFixedHeight(44 if collapsed else 58)
        self.update()

    def refresh(self, jobs: List[downloads.DownloadJob]):
        """Показывает первую идущую загрузку, а если всё скачано — недавно завершённую (ненадолго)."""
        active = [j for j in jobs if j.finished is None]
        recent = [j for j in jobs if j.finished is not None and time.time() - j.finished < DONE_VISIBLE_SECONDS]
        self.job = (active or recent or [None])[0]
        self.count = len(active)
        self.setVisible(self.job is not None)
        if self.job:
            self.setToolTip(f"<b>{self.job.title}</b><br>{job_status_line(self.job)}")
        self.update()

    def paintEvent(self, event):
        job = self.job
        if not job:
            return
        t = self.tokens
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(qcolor(t["border"]))
        p.setBrush(qcolor(t["surface2"] if self._hover else t["surface"]))
        p.drawRoundedRect(rect, 10, 10)
        done = job.finished is not None
        failed = job.state in (downloads.FAILED, downloads.CANCELLED)
        color = t["danger"] if failed else t["success"] if done else t["warning"] if job.paused else t["accent"]
        fraction = 1.0 if done else job.fraction()
        icon_name = "alert" if failed else "check_circle" if done else "download"
        if self.collapsed:
            p.drawPixmap(int(rect.center().x()) - 9, 8, icons.pixmap(icon_name, color, 18))
        else:
            p.drawPixmap(12, 11, icons.pixmap(icon_name, color, 18))
            fm = QFontMetrics(self.font())
            title = job.title if self.count <= 1 else tr("{0} и ещё {1}", job.title, self.count - 1)
            if not done:
                title = f"{int(fraction * 100)}%  ·  {title}"
            p.setPen(qcolor(t["text"]))
            p.drawText(QRectF(38, 6, rect.width() - 46, 20), Qt.AlignmentFlag.AlignVCenter,
                       fm.elidedText(title, Qt.TextElideMode.ElideRight, int(rect.width()) - 46))
            if done:
                second = job.summary or tr("Готово")
            else:
                second = downloads.status_text(job) or tr("{0}/с · {1}", format_size(job.speed()),
                                                           downloads.format_eta(job.eta()))
            p.setPen(qcolor(t["text_muted"]))
            p.drawText(QRectF(38, 25, rect.width() - 46, 18), Qt.AlignmentFlag.AlignVCenter,
                       fm.elidedText(second, Qt.TextElideMode.ElideRight, int(rect.width()) - 46))
        track = QRectF(10, rect.height() - 10, rect.width() - 20, 4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcolor(t["surface3"]))
        p.drawRoundedRect(track, 2, 2)
        p.setBrush(qcolor(color))
        p.drawRoundedRect(QRectF(track.x(), track.y(), max(4.0, track.width() * fraction), track.height()), 2, 2)
        p.end()

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


# --- График скорости ---

class SpeedChart(QWidget):
    """Скорость по секундам за последние полторы минуты — как график сети в Steam."""
    HEIGHT = 110

    def __init__(self, tokens: dict, parent=None):
        super().__init__(parent)
        self.tokens = tokens
        self.values: List[float] = []
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_values(self, values: List[float]):
        self.values = list(values)
        self.update()

    def paintEvent(self, event):
        t = self.tokens
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(qcolor(t["border"]))
        for i in range(1, 4):  # Сетка
            p.drawLine(0, int(h * i / 4), w, int(h * i / 4))
        if len(self.values) < 2:
            p.setPen(qcolor(t["text_faint"]))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, tr("График появится через пару секунд"))
            p.end()
            return
        top = max(self.values) or 1
        slots = downloads.HISTORY_SECONDS - 1
        step = w / slots
        start = w - (len(self.values) - 1) * step  # Новые значения — справа
        points = [(start + i * step, h - 4 - (h - 22) * v / top) for i, v in enumerate(self.values)]
        line = QPainterPath()
        line.moveTo(*points[0])
        for x, y in points[1:]:
            line.lineTo(x, y)
        area = QPainterPath(line)
        area.lineTo(points[-1][0], h)
        area.lineTo(points[0][0], h)
        area.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcolor(t["accent"], 0.18))
        p.drawPath(area)
        pen = p.pen()
        pen.setStyle(Qt.PenStyle.SolidLine)
        pen.setColor(qcolor(t["accent"]))
        pen.setWidthF(2.0)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(line)
        p.setPen(qcolor(t["text_faint"]))
        p.drawText(QRectF(6, 2, w - 12, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   tr("пик {0}/с", format_size(top)))
        p.end()


# --- Список файлов ---

class FileListModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.files: List[downloads.DownloadFile] = []

    def set_files(self, files: List[downloads.DownloadFile]):
        if len(files) == len(self.files):
            self.files = files
            if files:
                self.dataChanged.emit(self.index(0), self.index(len(files) - 1))
        else:
            self.beginResetModel()
            self.files = files
            self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.files)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self.files[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return item
        if role == Qt.ItemDataRole.ToolTipRole:
            return item.error or item.name
        return None


class FileDelegate(QStyledItemDelegate):
    ROW = 40
    STATES = {downloads.QUEUED: tr("в очереди"), downloads.ACTIVE: tr("качается"), downloads.DONE: tr("готово"),
              downloads.FAILED: tr("ошибка"), downloads.CANCELLED: tr("отменено")}

    def __init__(self, tokens: dict, parent=None):
        super().__init__(parent)
        self.tokens = tokens

    def sizeHint(self, option, index):
        return QSize(200, self.ROW)

    def paint(self, p: QPainter, option, index):
        item: downloads.DownloadFile = index.data(Qt.ItemDataRole.UserRole)
        if item is None:
            return
        t = self.tokens
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect).adjusted(4, 2, -4, -2)
        fm = QFontMetrics(option.font)
        color = {downloads.ACTIVE: t["accent"], downloads.DONE: t["success"], downloads.FAILED: t["danger"]}.get(
            item.state, t["text_faint"])
        if item.size:
            size_text = (f"{format_size(item.done)} / {format_size(item.size)}" if item.state == downloads.ACTIVE
                         else format_size(item.size))
        else:
            size_text = format_size(item.done) if item.done else ""
        right = f"{size_text}   {self.STATES.get(item.state, '')}".strip()
        right_w = fm.horizontalAdvance(right) + 8
        p.setPen(qcolor(t["text"] if item.state in (downloads.ACTIVE, downloads.FAILED) else t["text_muted"]))
        name = item.name if item.state != downloads.FAILED else f"{item.name} — {item.error}"
        p.drawText(QRectF(rect.x() + 6, rect.y(), rect.width() - right_w - 12, 22), Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(name, Qt.TextElideMode.ElideRight, int(rect.width() - right_w - 12)))
        p.setPen(qcolor(color if item.state != downloads.QUEUED else t["text_faint"]))
        p.drawText(QRectF(rect.right() - right_w, rect.y(), right_w, 22),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, right)
        track = QRectF(rect.x() + 6, rect.y() + 26, rect.width() - 12, 4)
        fraction = 1.0 if item.state == downloads.DONE else (item.done / item.size if item.size else 0.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcolor(t["surface3"]))
        p.drawRoundedRect(track, 2, 2)
        if fraction > 0:
            p.setBrush(qcolor(color, 1.0 if item.state == downloads.ACTIVE else 0.55))
            p.drawRoundedRect(QRectF(track.x(), track.y(), max(3.0, track.width() * min(1.0, fraction)), 4), 2, 2)
        p.restore()


# --- Страница ---

class _Tile(QFrame):
    def __init__(self, caption: str):
        super().__init__()
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)
        self.value = label("—", "SectionTitle")
        layout.addWidget(self.value)
        layout.addWidget(label(caption, "Muted", wrap=True))
        self.setMinimumWidth(130)


class DownloadsPage(QWidget):
    def __init__(self, tokens: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.tokens = tokens
        self.selected: Optional[downloads.DownloadJob] = None
        self._picked = False  # Пользователь сам выбрал загрузку в списке слева
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 20)
        root.setSpacing(14)
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(4)
        titles.addWidget(label(tr("Загрузки"), "PageTitle"))
        self.subtitle = label(tr("Что качается сейчас и что скачано за этот запуск программы."), "PageSubtitle", wrap=True)
        titles.addWidget(self.subtitle)
        head.addLayout(titles, 1)
        self.clear_btn = button(tr("Очистить завершённые"), None, "trash", tokens, "text_muted")
        self.clear_btn.clicked.connect(self._clear_finished)
        head.addWidget(self.clear_btn, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(head)

        self.empty = EmptyState()
        self.empty.set_content(tokens, "download", tr("Сейчас ничего не качается"),
                               tr("Здесь появятся обновления модов и установка готовых сборок: скорость, сколько осталось "
                                  "и каждый файл."))
        root.addWidget(self.empty, 1)

        self.body = QWidget()
        body = QHBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(14)
        root.addWidget(self.body, 1)

        self.jobs = QListWidget()
        self.jobs.setObjectName("SettingsNav")
        self.jobs.setFixedWidth(240)
        self.jobs.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.jobs.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.jobs.setWordWrap(False)
        self.jobs.currentRowChanged.connect(self._on_job_selected)
        body.addWidget(self.jobs)

        detail = QVBoxLayout()
        detail.setSpacing(12)
        card = QFrame()
        card.setObjectName("Card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 14, 18, 16)
        card_layout.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.job_title = label("", "SectionTitle")
        top.addWidget(self.job_title, 1)
        self.up_btn = button(tr("Раньше"), None, "arrow_up_circle", tokens, "text_muted", tr("Начать эту загрузку раньше"))
        self.up_btn.clicked.connect(lambda: self._move(-1))
        self.down_btn = button(tr("Позже"), None, "chevron_down", tokens, "text_muted", tr("Пропустить вперёд другие"))
        self.down_btn.clicked.connect(lambda: self._move(1))
        self.pause_btn = button(tr("Пауза"), None, "pause", tokens, "text")
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.retry_btn = button(tr("Повторить неудавшиеся"), "PrimaryButton", "refresh", tokens, "accent_text")
        self.retry_btn.clicked.connect(self._retry)
        self.cancel_btn = button(tr("Отменить"), "DangerButton", "x", tokens, "danger")
        self.cancel_btn.clicked.connect(self._cancel)
        for widget in (self.up_btn, self.down_btn, self.pause_btn, self.retry_btn, self.cancel_btn):
            top.addWidget(widget)
        card_layout.addLayout(top)
        self.job_status = label("", "Muted", wrap=True)
        card_layout.addWidget(self.job_status)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1000)
        self.bar.setFixedHeight(10)
        card_layout.addWidget(self.bar)
        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.tiles = {key: _Tile(caption) for key, caption in (
            ("bytes", tr("скачано")), ("speed", tr("скорость")), ("eta", tr("осталось")), ("files", tr("файлов")))}
        for tile in self.tiles.values():
            tiles.addWidget(tile)
        card_layout.addLayout(tiles)
        self.chart = SpeedChart(tokens)
        card_layout.addWidget(self.chart)
        detail.addWidget(card)

        self.files_model = FileListModel(self)
        self.files = QListView()
        self.files.setObjectName("ModList")
        self.files.setModel(self.files_model)
        self.delegate = FileDelegate(tokens, self.files)
        self.files.setItemDelegate(self.delegate)
        self.files.setUniformItemSizes(True)
        self.files.setSelectionMode(QListView.SelectionMode.NoSelection)
        self.files.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        detail.addWidget(self.files, 1)
        body.addLayout(detail, 1)
        self.refresh()

    def set_tokens(self, tokens: dict):
        self.tokens = tokens
        self.chart.tokens = tokens
        self.delegate.tokens = tokens
        refresh_button_icons(self, tokens)
        self.update()

    def select(self, job: Optional[downloads.DownloadJob]):
        self.selected = job
        self.refresh()

    def refresh(self):
        jobs = downloads.MANAGER.all()
        self.empty.setVisible(not jobs)
        self.body.setVisible(bool(jobs))
        self.clear_btn.setVisible(any(j.finished is not None for j in jobs))
        if not jobs:
            self.selected = None
            return
        running = next((j for j in jobs if j.finished is None), None)
        if self.selected not in jobs or (running and self.selected.finished is not None and not self._picked):
            self.selected = running or jobs[0]  # Новая загрузка важнее законченной, если пользователь не выбрал сам
        self.jobs.blockSignals(True)
        if self.jobs.count() != len(jobs):
            self.jobs.clear()
            for _ in jobs:
                self.jobs.addItem(QListWidgetItem())
        for row, job in enumerate(jobs):
            mark = ("⏸ " if job.paused else "") if job.finished is None else "✓ " if job.state == downloads.DONE else "✕ "
            percent = f"{int(job.fraction() * 100)}% · " if job.finished is None else ""
            item = self.jobs.item(row)
            item.setText(f"{mark}{job.title}\n{percent}{job_status_line(job)}")
            item.setData(Qt.ItemDataRole.UserRole, row)
        self.jobs.setCurrentRow(jobs.index(self.selected))
        self.jobs.blockSignals(False)
        self._show_job(self.selected)

    def _show_job(self, job: downloads.DownloadJob):
        totals = job.totals()
        running = job.finished is None
        self.job_title.setText(job.title)
        self.job_status.setText(job_status_line(job))
        self.cancel_btn.setVisible(running and job.cancel_callback is not None)
        waiting = job.state == downloads.WAITING
        self.up_btn.setVisible(waiting)
        self.down_btn.setVisible(waiting)
        self.pause_btn.setVisible(running and not waiting)
        self.pause_btn.setText(tr("Продолжить") if job.paused else tr("Пауза"))
        icon_name = "play" if job.paused else "pause"
        if self.pause_btn.property("icon_name") != icon_name:
            self.pause_btn.setProperty("icon_name", icon_name)
            self.pause_btn.setIcon(icons.icon(icon_name, self.tokens["text"], 16))
        self.retry_btn.setVisible(job.can_retry)
        self.bar.setValue(int((1.0 if job.state == downloads.DONE else job.fraction()) * 1000))
        size = format_size(totals["size"]) if totals["size"] else "?"
        self.tiles["bytes"].value.setText(f"{format_size(totals['bytes'])} / {size}")
        special = downloads.status_text(job)
        self.tiles["speed"].value.setText("—" if not running or special else f"{format_size(job.speed())}/с")
        self.tiles["eta"].value.setText(special or (downloads.format_eta(job.eta()) if running else "—"))
        files = f"{totals['files_done']} / {totals['files']}"
        if totals["failed"]:
            files += "  " + tr("ошибок: {0}", totals["failed"])
        self.tiles["files"].value.setText(files)
        self.chart.set_values(job.history)
        self.files_model.set_files(job.snapshot_files())

    def _on_job_selected(self, row: int):
        jobs = downloads.MANAGER.all()
        if 0 <= row < len(jobs):
            self.selected = jobs[row]
            self._picked = jobs[row].finished is not None or jobs[row].state == downloads.WAITING
            self._show_job(self.selected)

    def _toggle_pause(self):
        if self.selected:
            if self.selected.paused:
                self.selected.resume()
            else:
                self.selected.pause()
            self.refresh()

    def _retry(self):
        if self.selected:
            self.selected.retry()
            self.refresh()

    def _move(self, delta: int):
        if self.selected:
            downloads.MANAGER.move(self.selected, delta)
            self._picked = True
            self.refresh()

    def _cancel(self):
        if self.selected:
            self.selected.cancel()
            self.job_status.setText(tr("Отменяем…"))

    def _clear_finished(self):
        downloads.MANAGER.clear_finished()
        self.refresh()
