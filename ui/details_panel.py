"""Правая панель с подробностями о выбранном моде."""
import os
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import (
    QFormLayout, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QScrollArea,
    QSizePolicy, QTextBrowser, QVBoxLayout, QWidget,
)

import icons
from enums import ModStatus
from mod_updater_logic import format_date
from theme import rgba
from ui.widgets import button, label, refresh_button_icons
from view_model import status_of, status_style

SIDE_LABELS = {"required": "нужен", "optional": "необязателен", "unsupported": "не поддерживается"}
VERSION_TYPES = {"release": "релиз", "beta": "бета", "alpha": "альфа"}


def rounded_pixmap(pm: QPixmap, size: int, radius: int) -> QPixmap:
    result = QPixmap(pm.size())
    result.fill(Qt.GlobalColor.transparent)
    p = QPainter(result)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, pm.width(), pm.height(), radius * pm.width() / size, radius * pm.width() / size)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pm)
    p.end()
    return result


class DetailsPanel(QFrame):
    closed = pyqtSignal()
    install_requested = pyqtSignal(dict)
    delete_requested = pyqtSignal(dict)
    open_page_requested = pyqtSignal(dict)
    open_file_requested = pyqtSignal(dict)
    versions_requested = pyqtSignal(dict)
    version_install_requested = pyqtSignal(dict, dict)
    select_entry_requested = pyqtSignal(dict)

    def __init__(self, tokens: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("DetailsPanel")
        self.setFixedWidth(400)
        self.tokens = tokens
        self.entry: Optional[Dict] = None
        self._versions: List[Dict] = []

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
        self.versions_list.setMinimumHeight(180)
        self.versions_list.itemDoubleClicked.connect(self._install_selected_version)
        self.version_install_btn = button("Установить выбранную версию", None, "download", tokens, "text")
        self.version_install_btn.clicked.connect(self._install_selected_version)
        self.layout_.addWidget(self.versions_list)
        self.layout_.addWidget(self.version_install_btn)
        self.layout_.addStretch()
        self._set_versions_visible(False)

    def set_tokens(self, tokens: dict):
        self.tokens = tokens
        refresh_button_icons(self, tokens)
        if self.entry:
            self.set_entry(self.entry, None)

    # --- Заполнение ---

    def set_entry(self, entry: Dict, icon_pm: Optional[QPixmap]):
        t = self.tokens
        same_entry = self.entry is entry
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

        self._fill_info(entry)
        self._fill_dependencies(entry)

        changelog = (entry.get("_latest_info") or {}).get("changelog") if entry.get("new_version") else None
        self.changelog_title.setText(f"Что нового в {entry.get('new_version')}" if changelog else "Что нового")
        self.changelog_title.setVisible(bool(changelog))
        self.changelog.setVisible(bool(changelog))
        if changelog:
            self.changelog.setMarkdown(changelog[:20000])

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
            mark = "   ✓ установлена" if v.get("version_number") == current else ""
            item = QListWidgetItem(f"{v.get('version_number')}    ·  {vtype}  ·  {format_date(v.get('date_published'))}{mark}")
            item.setData(Qt.ItemDataRole.UserRole, v)
            self.versions_list.addItem(item)
        if not versions:
            self.versions_list.addItem("Нет версий для этой версии игры и загрузчика")
        self._set_versions_visible(True)

    def _install_selected_version(self, *_):
        item = self.versions_list.currentItem()
        version = item.data(Qt.ItemDataRole.UserRole) if item else None
        if self.entry and version:
            self.version_install_requested.emit(self.entry, version)
