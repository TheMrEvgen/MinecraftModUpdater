"""Правая панель с подробностями о выбранном моде."""
import os
from typing import Dict, List, Optional

from PyQt6.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QFontMetrics, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QFormLayout, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPlainTextEdit, QScrollArea,
    QSizePolicy, QStyle, QStyledItemDelegate, QTextBrowser, QVBoxLayout, QWidget,
)

import icons
from enums import ModStatus
from mod_updater_logic import format_date
from theme import qcolor, rgba
from ui.widgets import button, label, refresh_button_icons, rounded_pixmap
from query import days_since, is_abandoned
from view_model import entry_tags, format_size, status_of, status_style


def demote_headings(markdown: str) -> str:
    from ui.dialogs import demote_headings as demote  # dialogs импортирует widgets — избегаем цикла при загрузке
    return demote(markdown)

SIDE_LABELS = {"required": "нужен", "optional": "необязателен", "unsupported": "не поддерживается"}
VERSION_TYPES = {"release": "релиз", "beta": "бета", "alpha": "альфа"}
VERSION_TYPE_ROLES = {"release": "accent", "beta": "warning", "alpha": "danger"}
VERSION_ROLE = Qt.ItemDataRole.UserRole
INSTALLED_ROLE = Qt.ItemDataRole.UserRole + 1


class VersionDelegate(QStyledItemDelegate):
    """Строка версии в цветах темы: номер, цветная метка типа, дата и отметка установленной версии."""

    def __init__(self, view, tokens: dict):
        super().__init__(view)
        self.tokens = tokens
        base = view.font()
        self.font_bold = QFont(base)
        self.font_bold.setWeight(QFont.Weight.DemiBold)
        self.font_small = QFont(base)
        self.font_small.setPointSizeF(max(base.pointSizeF() - 1, 7.5))
        self.font_chip = QFont(self.font_small)
        self.font_chip.setWeight(QFont.Weight.DemiBold)

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), 46)

    def paint(self, p: QPainter, option, index):
        t = self.tokens
        version = index.data(VERSION_ROLE)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(4, 2, -4, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(t["accent"], 0.12) if selected else qcolor(t["surface2"]))
            p.drawRoundedRect(QRectF(r), 8, 8)
        if not version:  # Строка-сообщение («нет версий»)
            p.setPen(qcolor(t["text_muted"]))
            p.drawText(r.adjusted(10, 0, -10, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       index.data(Qt.ItemDataRole.DisplayRole) or "")
            p.restore()
            return

        installed = bool(index.data(INSTALLED_ROLE))
        right = r.right() - 10
        if installed:
            fm = QFontMetrics(self.font_chip)
            text = "установлена"
            w = fm.horizontalAdvance(text) + 30
            chip = QRectF(right - w, r.center().y() - 11, w, 22)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(t["accent"], 0.14))
            p.drawRoundedRect(chip, 11, 11)
            p.drawPixmap(QRect(int(chip.left()) + 8, int(chip.center().y()) - 6, 12, 12),
                         icons.pixmap("check", t["accent"], 12))
            p.setFont(self.font_chip)
            p.setPen(qcolor(t["accent"]))
            p.drawText(chip.adjusted(23, 0, -6, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
            right = int(chip.left()) - 8

        left = r.left() + 12
        width = max(20, right - left)
        p.setFont(self.font_bold)
        p.setPen(qcolor(t["text"]))
        fm_b = QFontMetrics(self.font_bold)
        p.drawText(QRect(left, r.top() + 5, width, fm_b.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   fm_b.elidedText(str(version.get("version_number") or ""), Qt.TextElideMode.ElideMiddle, width))

        vtype = version.get("version_type", "")
        color = t.get(VERSION_TYPE_ROLES.get(vtype, "text_muted"), t["text_muted"])
        p.setFont(self.font_small)
        fm_s = QFontMetrics(self.font_small)
        y = r.top() + 5 + fm_b.height() + 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcolor(color))
        p.drawEllipse(QRectF(left, y + fm_s.height() / 2 - 3, 6, 6))
        type_text = VERSION_TYPES.get(vtype, vtype)
        p.setPen(qcolor(color))
        p.drawText(QRect(left + 11, y, width, fm_s.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, type_text)
        date_x = left + 11 + fm_s.horizontalAdvance(type_text) + 10
        p.setPen(qcolor(t["text_faint"]))
        p.drawText(QRect(date_x, y, max(0, right - date_x), fm_s.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   format_date(version.get("date_published")))
        p.restore()


class DetailsPanel(QFrame):
    closed = pyqtSignal()
    install_requested = pyqtSignal(object)
    delete_requested = pyqtSignal(object)
    open_page_requested = pyqtSignal(object)
    open_file_requested = pyqtSignal(object)
    versions_requested = pyqtSignal(object)
    version_install_requested = pyqtSignal(object, object)
    select_entry_requested = pyqtSignal(object)
    pin_toggled = pyqtSignal(object)
    disable_toggled = pyqtSignal(object)
    tags_menu_requested = pyqtSignal(object, QPoint)
    favorite_toggled = pyqtSignal(object)
    note_changed = pyqtSignal(object, str)
    replacement_requested = pyqtSignal(object)   # Найти замену давно не обновлявшемуся моду

    def __init__(self, tokens: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("DetailsPanel")
        self.setFixedWidth(400)
        self.tokens = tokens
        self.entry: Optional[Dict] = None
        self._versions: List[Dict] = []
        self.custom_tags: Dict = {}  # Метки пользователя (ссылка на конфиг главного окна)
        self.show_abandoned = True

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        content = QWidget()
        content.setObjectName("DetailsContent")
        scroll.setWidget(content)
        self.layout_ = QVBoxLayout(content)
        self.layout_.setContentsMargins(18, 10, 14, 18)
        self.layout_.setSpacing(12)

        top = QHBoxLayout()
        self.favorite_btn = button("", "IconButton", "star", tokens, "text_muted", "В избранное")
        self.favorite_btn.setFixedSize(30, 30)
        self.favorite_btn.clicked.connect(lambda: self.entry and self.favorite_toggled.emit(self.entry))
        top.addWidget(self.favorite_btn)
        top.addStretch()
        self.close_btn = button("", "IconButton", "x", tokens, "text_muted", "Закрыть (Esc)")
        self.close_btn.setFixedSize(30, 30)
        self.close_btn.clicked.connect(self.closed)
        top.addWidget(self.close_btn)
        self.layout_.addLayout(top)

        header = QHBoxLayout()
        header.setSpacing(14)
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(64, 64)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(4)
        self.title = label("", "DetailsTitle", wrap=True)
        self.subtitle = label("", "Faint", wrap=True)
        for lbl in (self.title, self.subtitle):
            lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.status_pill = QLabel()
        self.status_pill.setObjectName("StatusPill")
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        titles.addWidget(self.status_pill, 0, Qt.AlignmentFlag.AlignLeft)
        header.addLayout(titles, 1)
        self.layout_.addLayout(header)

        self.description = label("", "Muted", wrap=True)
        self.layout_.addWidget(self.description)

        self.issues_label = label("", None, wrap=True)
        self.layout_.addWidget(self.issues_label)

        self.install_btn = button("Обновить", "PrimaryButton", "download", tokens, "accent_text")
        self.install_btn.setMinimumHeight(38)
        self.layout_.addWidget(self.install_btn)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.install_btn.clicked.connect(lambda: self.entry and self.install_requested.emit(self.entry))
        self.page_btn = button("Modrinth", None, "external", tokens, "text", "Открыть страницу мода")
        self.page_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.page_btn.clicked.connect(lambda: self.entry and self.open_page_requested.emit(self.entry))
        self.file_btn = button("В папке", None, "folder", tokens, "text", "Показать файл в папке")
        self.file_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.file_btn.clicked.connect(lambda: self.entry and self.open_file_requested.emit(self.entry))
        self.delete_btn = button("Удалить", "DangerButton", "trash", tokens, "danger", "Удалить мод (можно отменить)")
        self.delete_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.delete_btn.clicked.connect(lambda: self.entry and self.delete_requested.emit(self.entry))
        actions.addWidget(self.page_btn)
        actions.addWidget(self.file_btn)
        actions.addWidget(self.delete_btn)
        self.layout_.addLayout(actions)

        extra = QHBoxLayout()
        extra.setSpacing(8)
        self.pin_btn = button("Закрепить", None, "lock", tokens, "text", "Не обновлять этот мод (в том числе в «Обновить все»)")
        self.pin_btn.clicked.connect(lambda: self.entry and self.pin_toggled.emit(self.entry))
        self.disable_btn = button("Выключить", None, "power", tokens, "text", "Выключить мод без удаления (*.disabled)")
        self.disable_btn.clicked.connect(lambda: self.entry and self.disable_toggled.emit(self.entry))
        self.tags_btn = button("Метки", None, "tag", tokens, "text", "Добавить или убрать свои метки")
        self.tags_btn.clicked.connect(lambda: self.entry and self.tags_menu_requested.emit(
            self.entry, self.tags_btn.mapToGlobal(self.tags_btn.rect().bottomLeft())))
        for b in (self.pin_btn, self.disable_btn, self.tags_btn):
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            extra.addWidget(b)
        self.layout_.addLayout(extra)

        self.tags_label = QLabel()
        self.tags_label.setWordWrap(True)
        self.tags_label.setTextFormat(Qt.TextFormat.RichText)
        self.layout_.addWidget(self.tags_label)

        # Давно не обновлялся
        self.abandoned_box = QFrame()
        self.abandoned_box.setObjectName("Banner")
        box = QHBoxLayout(self.abandoned_box)
        box.setContentsMargins(12, 8, 8, 8)
        self.abandoned_label = label("", None, wrap=True)
        box.addWidget(self.abandoned_label, 1)
        self.replace_btn = button("Найти замену", "GhostButton", "compass", tokens, "text")
        self.replace_btn.clicked.connect(lambda: self.entry and self.replacement_requested.emit(self.entry))
        box.addWidget(self.replace_btn)
        self.layout_.addWidget(self.abandoned_box)

        # Заметка (сохраняется сама через полсекунды после ввода)
        self.note_title = label("Заметка", "SectionTitle")
        self.note_edit = QPlainTextEdit()
        self.note_edit.setPlaceholderText("Например: не обновлять до 1.21.4 — ломает мир")
        self.note_edit.setFixedHeight(64)
        self._note_timer = QTimer(self, singleShot=True, interval=500)
        self._note_timer.timeout.connect(self._save_note)
        self.note_edit.textChanged.connect(lambda: not self._loading_note and self._note_timer.start())
        self._loading_note = False
        self.layout_.addWidget(self.note_title)
        self.layout_.addWidget(self.note_edit)

        # Ссылки и галерея
        from ui.gallery import LinksRow
        self.links_title = label("Ссылки", "SectionTitle")
        self.links = LinksRow(tokens)
        self.layout_.addWidget(self.links_title)
        self.layout_.addWidget(self.links)
        self.gallery_title = label("Скриншоты", "SectionTitle")
        self.gallery = None  # Создаётся в attach_gallery: нужны загрузчики картинок главного окна
        self.layout_.addWidget(self.gallery_title)
        self._gallery_index = self.layout_.count()

        self.info_card = QFrame()
        self.info_card.setObjectName("Card")
        self.info_form = QFormLayout(self.info_card)
        self.info_form.setContentsMargins(14, 12, 14, 12)
        self.info_form.setHorizontalSpacing(16)
        self.info_form.setVerticalSpacing(8)
        self.info_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.layout_.addWidget(self.info_card)

        self.deps_title = label("Зависимости", "SectionTitle")
        self.deps_box = QVBoxLayout()
        self.deps_box.setSpacing(6)
        self.layout_.addWidget(self.deps_title)
        self.layout_.addLayout(self.deps_box)

        self.changelog_title = label("Что нового", "SectionTitle")
        self.changelog = QTextBrowser()
        self.changelog.setOpenExternalLinks(True)
        self.changelog.setMinimumHeight(160)
        self.changelog.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.layout_.addWidget(self.changelog_title)
        self.layout_.addWidget(self.changelog)

        versions_header = QHBoxLayout()
        versions_header.addWidget(label("Другие версии", "SectionTitle"))
        versions_header.addStretch()
        self.load_versions_btn = button("Показать", "GhostButton", "history", tokens, "text_muted")
        self.load_versions_btn.clicked.connect(lambda: self.entry and self.versions_requested.emit(self.entry))
        versions_header.addWidget(self.load_versions_btn)
        self.versions_header_widget = QWidget()
        self.versions_header_widget.setLayout(versions_header)
        versions_header.setContentsMargins(0, 0, 0, 0)
        self.layout_.addWidget(self.versions_header_widget)
        self.versions_list = QListWidget()
        self.versions_list.setObjectName("VersionList")
        self.versions_list.setMinimumHeight(220)
        self.versions_list.setMouseTracking(True)
        self.versions_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.versions_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.versions_delegate = VersionDelegate(self.versions_list, tokens)
        self.versions_list.setItemDelegate(self.versions_delegate)
        self.versions_list.itemDoubleClicked.connect(self._install_selected_version)
        self.version_install_btn = button("Установить выбранную версию", None, "download", tokens, "text")
        self.version_install_btn.clicked.connect(self._install_selected_version)
        self.layout_.addWidget(self.versions_list)
        self.layout_.addWidget(self.version_install_btn)
        self.layout_.addStretch()
        self._set_versions_visible(False)

    def attach_gallery(self, thumb_loader, full_loader):
        from ui.gallery import GalleryStrip
        self.gallery = GalleryStrip(thumb_loader, full_loader)
        self.layout_.insertWidget(self._gallery_index, self.gallery)
        self.gallery.hide()

    def _save_note(self):
        if self.entry is not None:
            self.note_changed.emit(self.entry, self.note_edit.toPlainText().strip())

    def set_tokens(self, tokens: dict):
        self.tokens = tokens
        self.links.tokens = tokens
        self.versions_delegate.tokens = tokens
        self.versions_list.viewport().update()
        refresh_button_icons(self, tokens)
        if self.entry:
            self.set_entry(self.entry, None)

    # --- Заполнение ---

    def set_entry(self, entry: Dict, icon_pm: Optional[QPixmap]):
        t = self.tokens
        same_entry = self.entry is entry
        if not same_entry and self._note_timer.isActive():  # Не теряем заметку, набранную прямо перед переключением
            self._note_timer.stop()
            self._save_note()
        self.entry = entry
        status = status_of(entry)

        if icon_pm and not icon_pm.isNull():
            self.icon_label.setPixmap(rounded_pixmap(icon_pm.scaled(
                64, 64, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation), 64, 14))
            self.icon_label.setStyleSheet("")
        elif not same_entry or self.icon_label.pixmap().isNull():
            self.icon_label.setPixmap(icons.pixmap("package", t["text_faint"], 30))
            self.icon_label.setStyleSheet(f"background: {t['surface3']}; border-radius: 14px;")

        self.title.setText(entry.get("name", ""))
        file_name = os.path.basename(entry.get("filepath") or "") or entry.get("new_filename") or ""
        self.subtitle.setText(file_name)
        text, role = status_style(entry)
        color = t.get(role, t["text_muted"])
        self.status_pill.setText(f"● {text}")
        self.status_pill.setStyleSheet(f"background: {rgba(color, 0.14)}; color: {color};")
        self.description.setText(entry.get("description") or "")
        self.description.setVisible(bool(entry.get("description")))

        issues = list(entry.get("_issues") or [])
        if entry.get("_required_by"):
            prefix = "Нужен для" if status == ModStatus.REQUIRED else "Используется модами"
            issues.append(f"{prefix}: {', '.join(entry['_required_by'])}")
        self.issues_label.setText("\n".join(f"• {i}" for i in issues))
        self.issues_label.setStyleSheet(f"color: {t['warning'] if entry.get('_issues') or status == ModStatus.REQUIRED else t['text_muted']};")
        self.issues_label.setVisible(bool(issues))

        can_install = bool(entry.get("download_url")) and status not in (ModStatus.DOWNLOADING, ModStatus.FINISHING)
        verb = "Обновить до" if entry.get("filepath") else "Установить"
        self.install_btn.setText(f"{verb} {entry.get('new_version') or ''}".strip())
        self.install_btn.setVisible(can_install)
        self.page_btn.setVisible(bool(entry.get("project_id")))
        self.file_btn.setVisible(bool(entry.get("filepath")))
        self.delete_btn.setVisible(bool(entry.get("filepath")))
        has_file = bool(entry.get("filepath"))
        self.pin_btn.setVisible(has_file)
        pinned = bool(entry.get("_pinned"))
        self.pin_btn.setText("Открепить" if pinned else "Закрепить")
        self.pin_btn.setProperty("icon_name", "unlock" if pinned else "lock")
        self.pin_btn.setIcon(icons.icon("unlock" if pinned else "lock", t["text"], 16))
        self.disable_btn.setVisible(has_file)
        disabled = bool(entry.get("disabled"))
        self.disable_btn.setText("Включить" if disabled else "Выключить")
        self.tags_btn.setVisible(bool(entry.get("project_id") or has_file))
        tags = entry_tags(entry, self.custom_tags)
        chips = []
        for key, title in tags:
            color = t["accent"] if key.startswith("user:") else t["text_muted"]
            chips.append(f"<span style='background:{t['surface3']}; color:{color};'>&nbsp;{title}&nbsp;</span>")
        self.tags_label.setText("&nbsp; ".join(chips))
        self.tags_label.setVisible(bool(chips))

        favorite = bool(entry.get("_favorite"))
        self.favorite_btn.setProperty("icon_name", "star_filled" if favorite else "star")
        self.favorite_btn.setProperty("icon_color_key", "warning" if favorite else "text_muted")
        self.favorite_btn.setIcon(icons.icon("star_filled" if favorite else "star", t["warning" if favorite else "text_muted"], 16))
        self.favorite_btn.setToolTip("Убрать из избранного" if favorite else "В избранное")
        self.favorite_btn.setVisible(bool(has_file or entry.get("project_id")))

        age = days_since(entry.get("project_updated"))
        abandoned = bool(entry.get("filepath")) and is_abandoned(entry)
        if abandoned:
            years = age / 365
            when = f"{years:.1f} года".replace(".", ",") if years < 5 else "больше 5 лет"
            self.abandoned_label.setText(f"Автор не обновлял мод {when}. Он может не работать с новыми версиями игры.")
        self.abandoned_box.setVisible(abandoned and self.show_abandoned)

        if not same_entry:
            self._note_timer.stop()
            self._loading_note = True
            self.note_edit.setPlainText(entry.get("_note") or "")
            self._loading_note = False
        self.note_title.setVisible(has_file or bool(entry.get("project_id")))
        self.note_edit.setVisible(has_file or bool(entry.get("project_id")))

        links = entry.get("links") or []
        self.links.set_links(links)
        self.links_title.setVisible(bool(links))
        gallery = entry.get("gallery") or []
        if self.gallery is not None and not same_entry:
            self.gallery.set_items(gallery)
        self.gallery_title.setVisible(bool(gallery) and self.gallery is not None)

        self._fill_info(entry)
        self._fill_dependencies(entry)

        changelog = (entry.get("_latest_info") or {}).get("changelog") if entry.get("new_version") else None
        self.changelog_title.setText(f"Что нового в {entry.get('new_version')}" if changelog else "Что нового")
        self.changelog_title.setVisible(bool(changelog))
        self.changelog.setVisible(bool(changelog))
        if changelog:
            self.changelog.setMarkdown(demote_headings(changelog[:20000]))

        self.versions_header_widget.setVisible(bool(entry.get("project_id")))
        if not same_entry:
            self._versions = []
            self._set_versions_visible(False)

    def _fill_info(self, entry: Dict):
        while self.info_form.rowCount():
            self.info_form.removeRow(0)
        info = entry.get("installed_info") or {}
        rows = []
        if entry.get("current_version"):
            rows.append(("Установлена", entry["current_version"]))
        if info.get("date_published"):
            rows.append(("Выпущена", format_date(info["date_published"])))
        if entry.get("new_version"):
            date = f"  ·  {entry['new_version_date']}" if entry.get("new_version_date") else ""
            rows.append(("Доступна", f"{entry['new_version']}{date}"))
        sides = [f"клиент — {SIDE_LABELS.get(entry.get('client_side'), '?')}",
                 f"сервер — {SIDE_LABELS.get(entry.get('server_side'), '?')}"]
        if entry.get("client_side") or entry.get("server_side"):
            rows.append(("Сторона", "\n".join(sides)))
        size = entry.get("size") or 0
        if size:
            new_size = entry.get("new_size") or 0
            rows.append(("Размер", format_size(size) + (f"  →  {format_size(new_size)}" if new_size and entry.get("new_version") else "")))
        elif entry.get("new_size"):
            rows.append(("Размер", format_size(entry["new_size"])))
        if entry.get("_pinned"):
            rows.append(("Закреплён", "обновления не предлагаются"))
        if entry.get("disabled"):
            rows.append(("Выключен", "файл переименован в *.disabled — игра его не загружает"))
        if entry.get("project_id"):
            rows.append(("ID проекта", entry["project_id"]))
        for key, value in rows:
            key_label = label(key, "Faint")
            value_label = label(str(value), None, wrap=True)
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.info_form.addRow(key_label, value_label)
        self.info_card.setVisible(bool(rows))

    def _fill_dependencies(self, entry: Dict):
        while self.deps_box.count():
            item = self.deps_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        deps = entry.get("dependencies") or []
        self.deps_title.setVisible(bool(deps))
        t = self.tokens
        for dep in deps:
            row = QFrame()
            row.setObjectName("Card")
            lay = QHBoxLayout(row)
            lay.setContentsMargins(12, 8, 8, 8)
            text, role = status_style(dep)
            color = t.get(role, t["text_muted"])
            kind = {"required": "обязательная", "optional": "необязательная",
                    "incompatible": "несовместимая"}.get(dep.get("type"), "")
            name = label(f"<b>{dep.get('name', '?')}</b><br><span style='color:{t['text_faint']}'>{kind}</span>")
            name.setTextFormat(Qt.TextFormat.RichText)
            lay.addWidget(name, 1)
            status_label = QLabel(text)
            status_label.setStyleSheet(f"color: {color}; font-weight: 600; background: transparent;")
            lay.addWidget(status_label)
            if dep.get("download_url") and not dep.get("filepath"):
                install = button("", "IconButton", "download", t, "accent", "Установить зависимость")
                install.clicked.connect(lambda _=False, d=dep: self.install_requested.emit(d))
                lay.addWidget(install)
            elif dep.get("filepath"):
                go = button("", "IconButton", "external", t, "text_muted", "Перейти к моду в списке")
                go.clicked.connect(lambda _=False, d=dep: self.select_entry_requested.emit(d))
                lay.addWidget(go)
            self.deps_box.addWidget(row)

    # --- Версии ---

    def _set_versions_visible(self, visible: bool):
        self.versions_list.setVisible(visible)
        self.version_install_btn.setVisible(visible)
        self.load_versions_btn.setText("Обновить список" if visible else "Показать")

    def set_versions(self, versions: List[Dict]):
        self._versions = versions
        self.versions_list.clear()
        current = (self.entry or {}).get("current_version")
        for v in versions:
            vtype = VERSION_TYPES.get(v.get("version_type"), v.get("version_type", ""))
            item = QListWidgetItem(f"{v.get('version_number')} · {vtype} · {format_date(v.get('date_published'))}")
            item.setData(VERSION_ROLE, v)
            item.setData(INSTALLED_ROLE, v.get("version_number") == current)
            self.versions_list.addItem(item)
        if not versions:
            self.versions_list.addItem("Нет версий для этой версии игры и загрузчика")
        self._set_versions_visible(True)

    def _install_selected_version(self, *_):
        item = self.versions_list.currentItem()
        version = item.data(VERSION_ROLE) if item else None
        if self.entry and version:
            self.version_install_requested.emit(self.entry, version)
