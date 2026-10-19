"""Переиспользуемые виджеты интерфейса."""
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QFont, QFontMetrics, QPainter
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QFrame, QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QStyle, QStyledItemDelegate, QVBoxLayout,
    QWidget,
)

import icons
from launchers import SOURCE_LABELS
from theme import qcolor


def label(text: str = "", object_name: Optional[str] = None, wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    if object_name:
        lbl.setObjectName(object_name)
    lbl.setWordWrap(wrap)
    return lbl


def button(text: str = "", object_name: Optional[str] = None, icon_name: Optional[str] = None,
           tokens: Optional[dict] = None, icon_color_key: str = "text", tooltip: str = "") -> QPushButton:
    btn = QPushButton(text)
    if object_name:
        btn.setObjectName(object_name)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if icon_name and tokens:
        btn.setIcon(icons.icon(icon_name, tokens[icon_color_key], 16))
        btn.setProperty("icon_name", icon_name)
        btn.setProperty("icon_color_key", icon_color_key)
    if tooltip:
        btn.setToolTip(tooltip)
    return btn


def refresh_button_icons(root: QWidget, tokens: dict):
    """Перекрашивает иконки кнопок после смены темы."""
    for btn in root.findChildren(QPushButton):
        name = btn.property("icon_name")
        if name:
            btn.setIcon(icons.icon(name, tokens[btn.property("icon_color_key") or "text"], 16))


def add_shadow(widget: QWidget, tokens: dict, blur: int = 28, alpha: float = 0.35):
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, 6)
    effect.setColor(qcolor(tokens["shadow"], alpha))
    widget.setGraphicsEffect(effect)


def repolish(widget: QWidget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class SearchBox(QLineEdit):
    def __init__(self, tokens: dict, placeholder: str, parent=None):
        super().__init__(parent)
        self.setObjectName("SearchBox")
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)
        self._icon_label = QLabel(self)
        self._icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.set_tokens(tokens)

    def set_tokens(self, tokens: dict):
        self._icon_label.setPixmap(icons.pixmap("search", tokens["text_faint"], 16))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._icon_label.setGeometry(12, (self.height() - 16) // 2, 16, 16)


class StatCard(QPushButton):
    """Карточка-сводка с числом; одновременно фильтр списка."""

    def __init__(self, title: str, category: str, color_key: str, tokens: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("StatCard")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.category = category
        self.color_key = color_key
        self.tokens = tokens
        self.title = title
        self.count = 0
        self.setFixedHeight(78)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_count(self, count: int):
        self.count = count
        self.update()

    def set_tokens(self, tokens: dict):
        self.tokens = tokens
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        t = self.tokens
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect().adjusted(16, 10, -16, -10)
        number_font = QFont(self.font())
        number_font.setPointSizeF(self.font().pointSizeF() + 7)
        number_font.setWeight(QFont.Weight.Bold)
        small = QFont(self.font())
        small.setPointSizeF(self.font().pointSizeF() - 0.5)
        fm, fm_small = QFontMetrics(number_font), QFontMetrics(small)
        active = self.count > 0 and self.color_key not in ("text",)
        top = r.top() + max(0, (r.height() - fm.height() - fm_small.height()) // 2)
        p.setFont(number_font)
        p.setPen(qcolor(t[self.color_key] if active else t["text"]))
        p.drawText(QRect(r.left(), top, r.width(), fm.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   str(self.count))
        p.setFont(small)
        p.setPen(qcolor(t["text_muted"]))
        p.drawText(QRect(r.left(), top + fm.height(), r.width(), fm_small.height()),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   fm_small.elidedText(self.title, Qt.TextElideMode.ElideRight, r.width()))
        p.end()


class ChipButton(QPushButton):
    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setObjectName("Chip")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_variant(self, accent: bool = False, warning: bool = False):
        self.setProperty("accent", "true" if accent else "false")
        self.setProperty("warning", "true" if warning else "false")
        repolish(self)


class ClickableLabel(QLabel):
    clicked = pyqtSignal()

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class ElidedLabel(ClickableLabel):
    """Подпись, которая сокращает длинный текст (путь) с многоточием посередине."""

    def __init__(self, text: str = "", parent=None):
        super().__init__("", parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setFullText(self, text: str):
        self._full = text
        self.setToolTip(text)
        self._update_elide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elide()

    def _update_elide(self):
        super().setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideMiddle, max(20, self.width())))


# --- Уведомления (toasts) ---

class Toast(QFrame):
    closed = pyqtSignal(object)

    KIND_ICONS = {"info": ("info", "info"), "success": ("check_circle", "success"),
                  "warning": ("alert", "warning"), "error": ("error", "danger")}

    def __init__(self, parent: QWidget, tokens: dict, text: str, kind: str, action_text: Optional[str],
                 action: Optional[Callable], timeout: int):
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setFixedWidth(380)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 12, 10, 12)
        layout.setSpacing(10)
        icon_name, color_key = self.KIND_ICONS.get(kind, self.KIND_ICONS["info"])
        icon_label = QLabel()
        icon_label.setPixmap(icons.pixmap(icon_name, tokens[color_key], 20))
        icon_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(icon_label)
        text_label = QLabel(text)
        text_label.setWordWrap(True)
        layout.addWidget(text_label, 1)
        if action_text and action:
            action_btn = QPushButton(action_text)
            action_btn.setObjectName("ToastAction")
            action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            action_btn.clicked.connect(lambda: (action(), self.dismiss()))
            layout.addWidget(action_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        close_btn = QPushButton()
        close_btn.setObjectName("IconButton")
        close_btn.setIcon(icons.icon("x", tokens["text_muted"], 14))
        close_btn.setFixedSize(26, 26)
        close_btn.clicked.connect(self.dismiss)
        layout.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignTop)
        add_shadow(self, tokens, 30, 0.45)
        self.adjustSize()
        self._timer = QTimer(self, singleShot=True, interval=timeout)
        self._timer.timeout.connect(self.dismiss)
        if timeout > 0:
            self._timer.start()
        self._closing = False

    def enterEvent(self, event):
        self._timer.stop()  # Не закрываем, пока пользователь читает
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self._timer.interval() > 0:
            self._timer.start(2500)
        super().leaveEvent(event)

    def dismiss(self):
        if not self._closing:
            self._closing = True
            self.hide()
            self.closed.emit(self)
            self.deleteLater()


class ToastManager(QObject):
    """Стек уведомлений в правом нижнем углу окна."""

    def __init__(self, host: QWidget, tokens: dict):
        super().__init__(host)
        self.host = host
        self.tokens = tokens
        self.toasts: List[Toast] = []
        host.installEventFilter(self)

    def set_tokens(self, tokens: dict):
        self.tokens = tokens

    def show(self, text: str, kind: str = "info", action_text: Optional[str] = None,
             action: Optional[Callable] = None, timeout: int = 5000):
        toast = Toast(self.host, self.tokens, text, kind, action_text, action, timeout)
        toast.closed.connect(self._remove)
        self.toasts.append(toast)
        if len(self.toasts) > 4:
            self.toasts[0].dismiss()
        toast.show()
        toast.raise_()
        self._layout()

    def _remove(self, toast):
        if toast in self.toasts:
            self.toasts.remove(toast)
        self._layout()

    def _layout(self):
        y = self.host.height() - 20
        for toast in reversed(self.toasts):
            toast.adjustSize()
            y -= toast.height()
            toast.move(self.host.width() - toast.width() - 20, y)
            y -= 10

    def eventFilter(self, obj, event):
        if obj is self.host and event.type() == QEvent.Type.Resize:
            self._layout()
        return False


# --- Плавающая панель выделения ---

class SelectionBar(QFrame):
    update_clicked = pyqtSignal()
    delete_clicked = pyqtSignal()
    clear_clicked = pyqtSignal()

    def __init__(self, parent: QWidget, tokens: dict):
        super().__init__(parent)
        self.setObjectName("SelectionBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 8, 8, 8)
        layout.setSpacing(8)
        self.count_label = QLabel()
        self.count_label.setStyleSheet("font-weight: 700; background: transparent;")
        layout.addWidget(self.count_label)
        layout.addSpacing(10)
        self.update_btn = button("Обновить", "PrimaryButton", "download", tokens, "accent_text")
        self.delete_btn = button("Удалить", "DangerButton", "trash", tokens, "danger")
        self.clear_btn = button("Снять выделение", "GhostButton", "x", tokens, "text_muted")
        self.update_btn.clicked.connect(self.update_clicked)
        self.delete_btn.clicked.connect(self.delete_clicked)
        self.clear_btn.clicked.connect(self.clear_clicked)
        for btn in (self.update_btn, self.delete_btn, self.clear_btn):
            layout.addWidget(btn)
        add_shadow(self, tokens, 36, 0.5)
        self.hide()

    def set_state(self, checked: int, updatable: int, deletable: int, install_mode: bool):
        self.count_label.setText(f"Выбрано: {checked}")
        verb = "Установить" if install_mode else "Обновить"
        self.update_btn.setText(f"{verb} ({updatable})" if updatable else verb)
        self.update_btn.setEnabled(updatable > 0)
        self.delete_btn.setVisible(deletable > 0)
        self.adjustSize()


class DropOverlay(QFrame):
    def __init__(self, parent: QWidget, tokens: dict):
        super().__init__(parent)
        self.setObjectName("DropOverlay")
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label = QLabel()
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text_label = QLabel("Отпустите, чтобы добавить")
        self.text_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint_label = QLabel(".jar — добавить моды · .mrpack — импорт сборки · строка или ссылка — синхронизация")
        self.hint_label.setObjectName("Muted")
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.icon_label)
        layout.addWidget(self.text_label)
        layout.addWidget(self.hint_label)
        self.set_tokens(tokens)
        self.hide()

    def set_tokens(self, tokens: dict):
        self.icon_label.setPixmap(icons.pixmap("drop", tokens["accent"], 48))


class EmptyState(QWidget):
    """Пустое состояние: иконка, заголовок, пояснение и кнопки."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(10)
        self.icon_label = QLabel()
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title = label("", "SectionTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text = label("", "Muted", wrap=True)
        self.text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text.setFixedWidth(440)
        self.buttons = QHBoxLayout()
        self.buttons.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.icon_label)
        layout.addWidget(self.title)
        layout.addWidget(self.text, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(6)
        layout.addLayout(self.buttons)

    def set_content(self, tokens: dict, icon_name: str, title: str, text: str, buttons: List[QPushButton] = ()):
        self.icon_label.setPixmap(icons.pixmap(icon_name, tokens["text_faint"], 44))
        self.title.setText(title)
        self.text.setText(text)
        while self.buttons.count():
            item = self.buttons.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for btn in buttons:
            self.buttons.addWidget(btn)


# --- Боковая панель ---

PROFILE_ROLE = Qt.ItemDataRole.UserRole + 10


class ProfileDelegate(QStyledItemDelegate):
    def __init__(self, parent, tokens: dict):
        super().__init__(parent)
        self.tokens = tokens

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), 50)

    def paint(self, p: QPainter, option, index):
        t = self.tokens
        data = index.data(PROFILE_ROLE) or {}
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(6, 2, -6, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(t["accent"], 0.12) if selected else qcolor(t["surface2"]))
            p.drawRoundedRect(QRectF(r), 8, 8)

        dot = QRectF(r.left() + 12, r.center().y() - 4, 8, 8)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcolor(t["accent"] if selected else t["border_strong"]))
        p.drawEllipse(dot)

        name_font = QFont(option.font)
        name_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(name_font)
        p.setPen(qcolor(t["accent"] if selected else t["text"]))
        text_left = r.left() + 30
        width = r.width() - 36
        fm = QFontMetrics(name_font)
        p.drawText(QRect(text_left, r.top() + 7, width, fm.height()), Qt.AlignmentFlag.AlignLeft,
                   fm.elidedText(data.get("name", ""), Qt.TextElideMode.ElideRight, width))
        small = QFont(option.font)
        small.setPointSizeF(option.font.pointSizeF() - 1.5)
        p.setFont(small)
        p.setPen(qcolor(t["text_faint"]))
        sub = data.get("subtitle", "")
        p.drawText(QRect(text_left, r.top() + 9 + fm.height(), width, fm.height()), Qt.AlignmentFlag.AlignLeft,
                   QFontMetrics(small).elidedText(sub, Qt.TextElideMode.ElideRight, width))
        p.restore()


class Sidebar(QFrame):
    profile_selected = pyqtSignal(str)
    profile_context_menu = pyqtSignal(str, QPoint)
    add_profile_clicked = pyqtSignal()
    page_changed = pyqtSignal(int)

    PAGES = (("package", "Моды"), ("history", "История"), ("terminal", "Журнал"), ("settings", "Настройки"))

    def __init__(self, tokens: dict, app_version: str, parent=None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(250)
        self.tokens = tokens
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 16, 12, 14)
        layout.setSpacing(4)

        brand = QHBoxLayout()
        brand.setContentsMargins(6, 0, 0, 0)
        self.logo = QLabel()
        self.logo.setFixedSize(34, 34)
        self.logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand.addWidget(self.logo)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(label("Mod Updater", "AppTitle"))
        titles.addWidget(label(f"ВЕРСИЯ {app_version}".upper(), "AppVersion"))
        brand.addLayout(titles)
        brand.addStretch()
        layout.addLayout(brand)
        layout.addSpacing(18)

        header = QHBoxLayout()
        header.setContentsMargins(8, 0, 0, 0)
        header.addWidget(label("СБОРКИ", "SectionLabel"))
        header.addStretch()
        self.add_btn = button("", "IconButton", "plus", tokens, "text_muted", "Добавить сборку")
        self.add_btn.setFixedSize(28, 28)
        self.add_btn.clicked.connect(self.add_profile_clicked)
        header.addWidget(self.add_btn)
        layout.addLayout(header)

        self.profile_list = QListWidget()
        self.profile_list.setObjectName("ProfileList")
        self.profile_list.setMouseTracking(True)
        self.profile_list.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.profile_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.profile_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.profile_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.profile_delegate = ProfileDelegate(self.profile_list, tokens)
        self.profile_list.setItemDelegate(self.profile_delegate)
        self.profile_list.currentItemChanged.connect(self._on_current_changed)
        self.profile_list.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.profile_list, 1)

        layout.addSpacing(8)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons = []
        for i, (icon_name, text) in enumerate(self.PAGES):
            btn = button(f"  {text}", "NavButton", icon_name, tokens, "text_muted")
            btn.setCheckable(True)
            btn.setIconSize(QSize(17, 17))
            self.nav_group.addButton(btn, i)
            self.nav_buttons.append(btn)
            layout.addWidget(btn)
        self.nav_buttons[0].setChecked(True)
        self.nav_group.idClicked.connect(self._on_nav)
        self.set_tokens(tokens)

    def set_tokens(self, tokens: dict):
        self.tokens = tokens
        self.profile_delegate.tokens = tokens
        app_icon = QApplication.windowIcon()
        if not app_icon.isNull():
            self.logo.setPixmap(app_icon.pixmap(QSize(34, 34)))
        else:
            self.logo.setStyleSheet(f"background: {tokens['accent']}; border-radius: 10px;")
            self.logo.setPixmap(icons.pixmap("package", tokens["accent_text"], 20))
        self._refresh_nav_icons()
        self.profile_list.viewport().update()

    def _refresh_nav_icons(self):
        for btn in self.nav_buttons:
            color = self.tokens["accent"] if btn.isChecked() else self.tokens["text_muted"]
            btn.setIcon(icons.icon(btn.property("icon_name"), color, 17))

    def _on_nav(self, page: int):
        self._refresh_nav_icons()
        self.page_changed.emit(page)

    def set_page(self, page: int):
        self.nav_buttons[page].setChecked(True)
        self._refresh_nav_icons()

    def set_profiles(self, profiles: List[Dict], current: str):
        self.profile_list.blockSignals(True)
        self.profile_list.clear()
        for data in profiles:
            item = QListWidgetItem(data["name"])
            item.setData(PROFILE_ROLE, data)
            item.setToolTip(data.get("tooltip", ""))
            self.profile_list.addItem(item)
            if data["name"] == current:
                self.profile_list.setCurrentItem(item)
        self.profile_list.blockSignals(False)

    def _on_current_changed(self, current, previous):
        if current:
            self.profile_selected.emit(current.text())

    def _on_context_menu(self, pos):
        item = self.profile_list.itemAt(pos)
        if item:
            self.profile_context_menu.emit(item.text(), self.profile_list.viewport().mapToGlobal(pos))


def profile_subtitle(profile: Dict) -> str:
    parts = [profile.get("game_version") or "?"]
    loader = profile.get("loader")
    if loader:
        parts.append({"neoforge": "NeoForge", "fabric": "Fabric", "forge": "Forge", "quilt": "Quilt"}.get(loader, loader))
    source = profile.get("source", "manual")
    if source not in ("manual", None):
        parts.append(SOURCE_LABELS.get(source, source).split()[0])
    return " · ".join(parts)


def make_action(parent, tokens: dict, icon_name: str, text: str, slot: Callable, color_key: str = "text_muted") -> QAction:
    action = QAction(icons.icon(icon_name, tokens[color_key], 16), text, parent)
    action.triggered.connect(slot)
    return action


class FadeIn:
    """Плавное появление виджета."""

    @staticmethod
    def run(widget: QWidget, duration: int = 160):
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", widget)
        anim.setDuration(duration)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(lambda: widget.setGraphicsEffect(None))
        anim.start()
