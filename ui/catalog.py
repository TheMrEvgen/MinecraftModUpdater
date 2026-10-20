"""
Страница «Каталог»: поиск модов, шейдеров и ресурспаков на Modrinth — с фильтрами, панелью проекта
(описание, скриншоты, ссылки, версии, зависимости), отметками для массовой установки и меню строки.
"""
import webbrowser
from typing import Callable, Dict, List, Optional, Set

from PyQt6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMenu, QScrollArea, QSizePolicy, QStackedWidget, QStyle, QStyledItemDelegate, QTextBrowser,
    QVBoxLayout, QWidget,
)

import icons
from content import CONTENT_TYPES, MODS, ContentType
from mod_updater_logic import format_date, get_all_versions, get_categories, get_project, get_projects_info_from_ids, search_projects
from theme import qcolor
from ui.details_panel import PANEL_MAX, panel_width
from ui.widgets import (
    EmptyState, SearchBox, button, flex_button, label, make_action, refresh_button_icons, rounded_pixmap, style_segments,
)
from view_model import CATEGORY_LABELS, LOADER_CATEGORIES
from i18n import language, tr

HIT_ROLE = Qt.ItemDataRole.UserRole + 1   # индекс в CatalogPage.hits
PAGE_SIZE = 20
SORTS = (("relevance", tr("По релевантности")), ("downloads", tr("Популярные")), ("updated", tr("Недавно обновлённые")),
         ("newest", tr("Новые")), ("follows", tr("Больше подписчиков")))
SIDES = (("", tr("Любая сторона")), ("client", tr("Нужны на клиенте")), ("server", tr("Работают на сервере")))
CONTENT_ICONS = {"mods": "package", "shaders": "sparkles", "resourcepacks": "image"}
VERSION_TYPES = {"release": tr("релиз"), "beta": tr("бета"), "alpha": tr("альфа")}
SIDE_TEXT = {"required": tr("нужен"), "optional": tr("необязателен"), "unsupported": tr("не нужен"), "unknown": "?"}


def format_count(n: int) -> str:
    if n >= 1_000_000:
        return tr('{0:.1f} млн', n / 1_000_000).replace(".", "," if language() == "ru" else ".").replace(",0 ", " ").replace(".0", "")
    if n >= 1_000:
        return tr('{0:.0f} тыс.', n / 1_000)
    return str(n)


def hit_url(hit: Dict) -> str:
    return f"https://modrinth.com/{hit.get('project_type', 'mod')}/{hit.get('slug') or hit.get('project_id')}"


class CatalogDelegate(QStyledItemDelegate):
    """Карточка проекта: отметка, иконка, название, автор, описание, загрузки и метки, кнопка установки."""
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
    def narrow(rect: QRect) -> bool:
        return rect.width() < 560

    @classmethod
    def button_rect(cls, rect: QRect) -> QRect:
        if cls.narrow(rect):  # Узкий список: кнопка становится значком
            return QRect(rect.right() - 54, rect.center().y() - 17, 40, 34)
        return QRect(rect.right() - 150, rect.center().y() - 17, 134, 34)

    @staticmethod
    def check_rect(rect: QRect) -> QRect:
        return QRect(rect.left() + 16, rect.center().y() - 9, 18, 18)

    def paint(self, p: QPainter, option, index):
        hit = self.page.hit_at(index.data(HIT_ROLE))
        if not hit:
            return
        t = self.page.tokens
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected or hovered:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(t["accent"], 0.10) if selected else qcolor(t["surface2"]))
            p.drawRoundedRect(QRectF(rect.adjusted(4, 3, -4, -3)), 10, 10)
        if selected:
            p.setBrush(qcolor(t["accent"]))
            p.drawRoundedRect(QRectF(rect.left() + 4, rect.top() + 18, 3, rect.height() - 36), 1.5, 1.5)

        state = self.page.install_state(hit)
        check = self.check_rect(rect)
        if state == "available":
            checked = hit.get("project_id") in self.page.checked
            rf = QRectF(check).adjusted(0.75, 0.75, -0.75, -0.75)
            if checked:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(qcolor(t["accent"]))
                p.drawRoundedRect(rf, 5, 5)
                p.drawPixmap(check.adjusted(2, 2, -2, -2), icons.pixmap("check", t["accent_text"], 14))
            else:
                p.setPen(QPen(qcolor(t["accent"] if hovered and check.adjusted(-8, -12, 8, 12).contains(self._mouse)
                                     else t["border_strong"]), 1.5))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(rf, 5, 5)

        icon_rect = QRect(check.right() + 14, rect.center().y() - 26, 52, 52)
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
        p.setFont(self.font_title)
        p.setPen(qcolor(t["text"]))
        title = fm_t.elidedText(hit.get("title") or "", Qt.TextElideMode.ElideRight, width)
        p.drawText(QRect(left, y, width, fm_t.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        if hit.get("author"):
            x = left + fm_t.horizontalAdvance(title) + 8
            p.setFont(self.font_small)
            p.setPen(qcolor(t["text_faint"]))
            p.drawText(QRect(x, y, max(0, left + width - x), fm_t.height()),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       fm_s.elidedText(tr('от {0}', hit['author']), Qt.TextElideMode.ElideRight, max(0, left + width - x)))
        y += fm_t.height() + 3
        p.setFont(self.font_small)
        p.setPen(qcolor(t["text_muted"]))
        p.drawText(QRect(left, y, width, fm_s.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   fm_s.elidedText(" ".join((hit.get("description") or "").split()), Qt.TextElideMode.ElideRight, width))
        y += fm_s.height() + 7
        x = left
        p.drawPixmap(QRect(x, y + 1, 12, 12), icons.pixmap("download", t["text_faint"], 12))
        p.setPen(qcolor(t["text_faint"]))
        text = format_count(hit.get("downloads") or 0)
        p.drawText(QRect(x + 16, y, 100, fm_s.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        x += 16 + fm_s.horizontalAdvance(text) + 14
        categories = [c for c in hit.get("display_categories") or hit.get("categories") or [] if c not in LOADER_CATEGORIES]
        for cat in categories[:0 if self.narrow(rect) else 4]:
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

        hot = hovered and btn.contains(self._mouse)
        p.setFont(self.font_button)
        if self.narrow(rect):
            self._paint_icon_button(p, btn, state, hot)
        elif state == "installed":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(t["accent"], 0.12))
            p.drawRoundedRect(QRectF(btn), 9, 9)
            p.setPen(qcolor(t["accent"]))
            p.drawText(btn, Qt.AlignmentFlag.AlignCenter, tr("✓ Установлен"))
        elif state == "installing":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(t["surface3"]))
            p.drawRoundedRect(QRectF(btn), 9, 9)
            p.setPen(qcolor(t["text_muted"]))
            p.drawText(btn, Qt.AlignmentFlag.AlignCenter, tr("Установка…"))
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(t["accent_hover"] if hot else t["accent"]))
            p.drawRoundedRect(QRectF(btn), 9, 9)
            p.drawPixmap(QRect(btn.left() + 16, btn.center().y() - 8, 16, 16), icons.pixmap("download", t["accent_text"], 16))
            p.setPen(qcolor(t["accent_text"]))
            p.drawText(btn.adjusted(26, 0, 0, 0), Qt.AlignmentFlag.AlignCenter, tr("Установить"))
        p.restore()

    def _paint_icon_button(self, p: QPainter, btn: QRect, state: str, hot: bool):
        t = self.page.tokens
        p.setPen(Qt.PenStyle.NoPen)
        if state == "available":
            p.setBrush(qcolor(t["accent_hover"] if hot else t["accent"]))
            icon_name, color = "download", t["accent_text"]
        else:
            p.setBrush(qcolor(t["accent"], 0.12) if state == "installed" else qcolor(t["surface3"]))
            icon_name, color = ("check", t["accent"]) if state == "installed" else ("refresh", t["text_muted"])
        p.drawRoundedRect(QRectF(btn), 9, 9)
        p.drawPixmap(QRect(btn.center().x() - 8, btn.center().y() - 8, 16, 16), icons.pixmap(icon_name, color, 16))

    def helpEvent(self, event, view, option, index):
        hit = self.page.hit_at(index.data(HIT_ROLE))
        if hit and self.button_rect(option.rect).contains(event.pos()):
            state = self.page.install_state(hit)
            from PyQt6.QtWidgets import QToolTip
            QToolTip.showText(event.globalPos(), {"available": tr("Установить"), "installed": tr("Уже установлен"),
                                                  "installing": tr("Устанавливается…")}[state], view)
            return True
        return super().helpEvent(event, view, option, index)

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.Type.MouseMove:
            self._mouse = event.position().toPoint()
            self.page.list.viewport().update()
            return False
        if event.type() in (QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonPress) and \
                event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            hit = self.page.hit_at(index.data(HIT_ROLE))
            on_button = self.button_rect(option.rect).contains(pos)
            on_check = self.check_rect(option.rect).adjusted(-8, -12, 8, 12).contains(pos)
            if not hit or not (on_button or on_check):
                return False
            if event.type() == QEvent.Type.MouseButtonRelease:
                if on_button and self.page.install_state(hit) == "available":
                    self.page.install_requested.emit(hit)
                elif on_check:
                    self.page.toggle_checked(hit)
            return True  # Клики по кнопке и отметке не меняют выбранную строку
        return False


class CatalogDetails(QFrame):
    """Панель проекта из каталога: всё о проекте до установки."""
    closed = pyqtSignal()

    def __init__(self, page: "CatalogPage"):
        super().__init__()
        self.page = page
        self.setObjectName("DetailsPanel")
        self.setFixedWidth(PANEL_MAX)
        self.hit: Optional[Dict] = None
        self.versions: List[Dict] = []
        t = page.tokens
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        self.layout_ = QVBoxLayout(content)
        self.layout_.setContentsMargins(18, 10, 14, 18)
        self.layout_.setSpacing(12)
        top = QHBoxLayout()
        top.addStretch()
        close = button("", "IconButton", "x", t, "text_muted", tr("Закрыть (Esc)"))
        close.setFixedSize(30, 30)
        close.clicked.connect(self.closed)
        top.addWidget(close)
        self.layout_.addLayout(top)

        header = QHBoxLayout()
        header.setSpacing(14)
        self.icon = QLabel()
        self.icon.setFixedSize(64, 64)
        header.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(3)
        self.title = label("", "DetailsTitle", wrap=True)
        self.author = label("", "Faint", wrap=True)
        self.stats = label("", "Muted", wrap=True)
        for w in (self.title, self.author, self.stats):
            w.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            titles.addWidget(w)
        header.addLayout(titles, 1)
        self.layout_.addLayout(header)
        self.summary = label("", "Muted", wrap=True)
        self.layout_.addWidget(self.summary)

        from ui.widgets import ElideButton
        self.install_btn = ElideButton(tr("Установить"))
        self.install_btn.setObjectName("PrimaryButton")
        self.install_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.install_btn.setProperty("icon_name", "download")
        self.install_btn.setProperty("icon_color_key", "accent_text")
        self.install_btn.setIcon(icons.icon("download", t["accent_text"], 16))
        self.install_btn.setMinimumHeight(38)
        self.install_btn.clicked.connect(lambda: self.hit and self.page.install_requested.emit(self.hit))
        self.layout_.addWidget(self.install_btn)
        actions = QHBoxLayout()
        self.page_btn = flex_button("Modrinth", None, "external", t, "text", tr("Открыть страницу проекта"))
        self.page_btn.clicked.connect(lambda: self.hit and webbrowser.open(hit_url(self.hit)))
        self.mine_btn = flex_button(tr("В моих модах"), None, "package", t, "text", tr("Показать установленный мод в списке"))
        self.mine_btn.clicked.connect(lambda: self.hit and self.page.show_installed_requested.emit(self.hit))
        self.similar_btn = flex_button(tr("Похожие"), None, "compass", t, "text", tr("Искать проекты той же категории"))
        self.similar_btn.clicked.connect(lambda: self.hit and self.page.show_similar(self.hit))
        for b in (self.page_btn, self.mine_btn, self.similar_btn):
            actions.addWidget(b)
        self.layout_.addLayout(actions)

        self.info = label("", None, wrap=True)
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.layout_.addWidget(self.info)

        from ui.gallery import GalleryStrip, LinksRow
        self.gallery_title = label(tr("Скриншоты"), "SectionTitle")
        self.gallery = GalleryStrip(page.thumb_loader, page.full_loader) if page.thumb_loader else None
        if self.gallery:
            self.gallery.set_tokens(t)
        self.layout_.addWidget(self.gallery_title)
        if self.gallery:
            self.layout_.addWidget(self.gallery)
        self.links_title = label(tr("Ссылки"), "SectionTitle")
        self.links = LinksRow(t)
        self.layout_.addWidget(self.links_title)
        self.layout_.addWidget(self.links)

        self.deps_title = label(tr("Зависимости"), "SectionTitle")
        self.deps = label("", "Muted", wrap=True)
        self.deps.setTextFormat(Qt.TextFormat.RichText)
        self.layout_.addWidget(self.deps_title)
        self.layout_.addWidget(self.deps)

        self.body_title = label(tr("Описание"), "SectionTitle")
        self.body = QTextBrowser()
        self.body.setOpenExternalLinks(True)
        self.body.setMinimumHeight(260)
        self.layout_.addWidget(self.body_title)
        self.layout_.addWidget(self.body)

        self.versions_title = label(tr("Версии для вашей сборки"), "SectionTitle")
        self.versions_list = QListWidget()
        self.versions_list.setObjectName("VersionList")
        self.versions_list.setMinimumHeight(200)
        from ui.details_panel import VersionDelegate
        self.versions_delegate = VersionDelegate(self.versions_list, t)
        self.versions_list.setItemDelegate(self.versions_delegate)
        self.versions_list.itemDoubleClicked.connect(lambda _: self._install_version())
        self.version_btn = button(tr("Установить выбранную версию"), None, "download", t, "text")
        self.version_btn.clicked.connect(self._install_version)
        for w in (self.versions_title, self.versions_list, self.version_btn):
            self.layout_.addWidget(w)
        self.layout_.addStretch()

    def set_tokens(self, tokens: dict):
        self.versions_delegate.tokens = tokens
        self.links.tokens = tokens
        if self.gallery:
            self.gallery.set_tokens(tokens)
        refresh_button_icons(self, tokens)

    def show_hit(self, hit: Dict):
        from ui.details_panel import demote_headings
        t = self.page.tokens
        same = self.hit is not None and self.hit.get("project_id") == hit.get("project_id")
        self.hit = hit
        pm = self.page.icon_provider({"project_id": hit.get("project_id"), "icon_url": hit.get("icon_url")})
        if pm and not pm.isNull():
            self.icon.setPixmap(rounded_pixmap(pm.scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatio,
                                                         Qt.TransformationMode.SmoothTransformation), 64, 14))
        else:
            self.icon.setPixmap(icons.pixmap("package", t["text_faint"], 30))
        self.title.setText(hit.get("title") or "")
        self.author.setText(tr('от {0}', hit['author']) if hit.get("author") else "")
        self.stats.setText(tr('↓ {0}   ♥ {1}   ·   обновлён {2}', format_count(hit.get('downloads') or 0), format_count(hit.get('follows') or 0), format_date(hit.get('date_modified'))))
        self.summary.setText(hit.get("description") or "")
        state = self.page.install_state(hit)
        self.install_btn.setVisible(state == "available")
        self.mine_btn.setVisible(state == "installed")
        cats = [CATEGORY_LABELS.get(c, c) for c in hit.get("display_categories") or hit.get("categories") or []
                if c not in LOADER_CATEGORIES]
        side = (tr('клиент — {0}, сервер — {1}', SIDE_TEXT.get(hit.get('client_side'), '?'), SIDE_TEXT.get(hit.get('server_side'), '?')))
        self.info.setText(tr("<span style='color:{0}'>Категории:</span> {1}<br><span style='color:{2}'>Сторона:</span> {3}<br><span style='color:{4}'>Лицензия:</span> {5}", t['text_faint'], ', '.join(cats) or '—', t['text_faint'], side, t['text_faint'], hit.get('license') or '—'))
        if same:
            return
        self.body.setMarkdown(tr("_Загружаем описание…_"))
        self.deps.setText(tr("Загружаем…"))
        for w in (self.gallery_title, self.links_title, self.links):
            w.hide()
        if self.gallery:
            self.gallery.hide()
        self.versions_list.clear()
        pid = hit.get("project_id")
        self.page.run_task(get_project, (pid,), lambda project: self._on_project(pid, project, demote_headings),
                           lambda e: self.body.setMarkdown(tr('_Не удалось загрузить описание: {0}_', e)), None)
        loaders = self.page.content.loaders(self.page.loader) if self.page.content is MODS else None
        self.page.run_task(get_all_versions, (pid, self.page.game_version, loaders),
                           lambda versions: self._on_versions(pid, versions),
                           lambda e: self.deps.setText(tr('Не удалось загрузить версии: {0}', e)), None)

    def _on_project(self, pid: str, project: Dict, demote):
        if not self.hit or self.hit.get("project_id") != pid:
            return
        from scanner import project_links
        self.body.setMarkdown(demote((project.get("body") or project.get("description") or "")[:60000]))
        links = project_links(project)
        self.links.set_links(links)
        self.links_title.setVisible(bool(links))
        gallery = project.get("gallery") or []
        if self.gallery:
            self.gallery.set_items(gallery)
        self.gallery_title.setVisible(bool(gallery) and self.gallery is not None)

    def _on_versions(self, pid: str, versions: List[Dict]):
        if not self.hit or self.hit.get("project_id") != pid:
            return
        from ui.details_panel import INSTALLED_ROLE, VERSION_ROLE
        self.versions = versions
        self.versions_list.clear()
        installed_version = self.page.installed_version(pid)
        for v in versions[:50]:
            item = QListWidgetItem(v.get("version_number") or "")
            item.setData(VERSION_ROLE, v)
            item.setData(INSTALLED_ROLE, bool(installed_version) and v.get("version_number") == installed_version)
            self.versions_list.addItem(item)
        if not versions:
            self.versions_list.addItem(tr("Нет версий для этой версии игры и загрузчика"))
            self.install_btn.setEnabled(False)
            self.install_btn.setText(tr("Нет версии для вашей сборки"))
        else:
            self.install_btn.setEnabled(True)
            self.install_btn.setText(tr('Установить {0}', versions[0].get('version_number')))
        deps = [d for d in (versions[0].get("dependencies") if versions else []) or [] if d.get("project_id")]
        if not deps:
            self.deps.setText(tr("Не нужны."))
            return
        installed = self.page.installed.get(self.page.content.key, set())
        self.page.run_task(get_projects_info_from_ids, ([d["project_id"] for d in deps],),
                           lambda infos: self._show_deps(pid, deps, infos, installed), lambda e: None, None)

    def _show_deps(self, pid: str, deps: List[Dict], infos: Dict, installed: Set[str]):
        if not self.hit or self.hit.get("project_id") != pid:
            return
        t = self.page.tokens
        kinds = {"required": tr("обязательная"), "optional": tr("необязательная"), "incompatible": tr("несовместима"),
                 "embedded": tr("встроена")}
        lines = []
        for dep in deps:
            name = (infos.get(dep["project_id"]) or {}).get("title") or dep["project_id"]
            mark = (tr("<span style='color:{0}'>✓ установлена</span>", t['accent']) if dep["project_id"] in installed else
                    tr("<span style='color:{0}'>поставится сама</span>", t['info']) if dep.get("dependency_type") == "required"
                    else "")
            lines.append(f"• <b>{name}</b> — {kinds.get(dep.get('dependency_type'), '')} {mark}")
        self.deps.setText("<br>".join(lines))

    def _install_version(self):
        from ui.details_panel import VERSION_ROLE
        item = self.versions_list.currentItem()
        version = item.data(VERSION_ROLE) if item else None
        if self.hit and version:
            self.page.install_version_requested.emit(self.hit, version)


class CatalogPage(QWidget):
    install_requested = pyqtSignal(object)            # hit из ответа /search
    install_many_requested = pyqtSignal(object)       # [hit]
    install_version_requested = pyqtSignal(object, object)  # hit, версия
    open_requested = pyqtSignal(object)
    show_installed_requested = pyqtSignal(object)

    def __init__(self, tokens: dict, run_task: Callable, icon_provider: Callable[[Dict], Optional[QPixmap]],
                 thumb_loader=None, full_loader=None, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.tokens = tokens
        self.run_task = run_task
        self.icon_provider = icon_provider
        self.thumb_loader, self.full_loader = thumb_loader, full_loader
        self.content: ContentType = MODS
        self.game_version = ""
        self.loader = ""
        self.installed: Dict[str, Set[str]] = {}     # тип контента -> project_id установленных
        self.installing: Set[str] = set()
        self.installed_version: Callable[[str], Optional[str]] = lambda project_id: None  # Задаёт главное окно
        self.checked: Dict[str, Dict] = {}           # project_id -> hit (отмеченные для массовой установки)
        self.hits: List[Dict] = []
        self.total = 0
        self._request = 0
        self._loading = False

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 18)
        root.setSpacing(12)
        head = QVBoxLayout()
        head.setSpacing(4)
        head.addWidget(label(tr("Каталог"), "PageTitle"))
        self.context_label = label("", "PageSubtitle", wrap=True)
        head.addWidget(self.context_label)
        root.addLayout(head)

        tabs = QHBoxLayout()
        tabs.setSpacing(6)
        self.tab_group = QButtonGroup(self)
        for i, content in enumerate(CONTENT_TYPES):
            tab = button(f" {content.title}", "Segment", CONTENT_ICONS[content.key], tokens, "text_muted")
            tab.setCheckable(True)
            self.tab_group.addButton(tab, i)
            tabs.addWidget(tab)
        self.tab_group.buttons()[0].setChecked(True)
        self.tab_group.idClicked.connect(lambda i: self._set_content(CONTENT_TYPES[i]))
        tabs.addStretch()
        root.addLayout(tabs)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.search = SearchBox(tokens, tr("Найти на Modrinth: название, описание…"))
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

        filters = QHBoxLayout()
        filters.setSpacing(10)
        self.category_combo = QComboBox()
        self.category_combo.setMinimumHeight(32)
        self.category_combo.addItem(tr("Все категории"), None)
        self.category_combo.currentIndexChanged.connect(self.refresh)
        self.side_combo = QComboBox()
        self.side_combo.setMinimumHeight(32)
        for key, title in SIDES:
            self.side_combo.addItem(title, key)
        self.side_combo.currentIndexChanged.connect(self.refresh)
        self.compatible = QCheckBox(tr("Только для моей версии игры"))
        self.compatible.setChecked(True)
        self.compatible.toggled.connect(self.refresh)
        self.hide_installed = QCheckBox(tr("Скрыть установленные"))
        self.hide_installed.toggled.connect(self._apply_hidden)
        for w in (self.category_combo, self.side_combo, self.compatible, self.hide_installed):
            filters.addWidget(w)
        filters.addStretch()
        root.addLayout(filters)

        body = QHBoxLayout()
        body.setSpacing(14)
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        self.list = QListWidget()
        self.list.setObjectName("ModList")
        self.list.setMinimumWidth(280)
        self.list.setMouseTracking(True)
        self.list.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.verticalScrollBar().setSingleStep(18)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setItemDelegate(CatalogDelegate(self))
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self.list.currentItemChanged.connect(self._on_current)
        self.list.itemDoubleClicked.connect(lambda item: self.open_requested.emit(self.hit_at(item.data(HIT_ROLE))))
        self.list.verticalScrollBar().valueChanged.connect(self._maybe_load_more)
        self.list.installEventFilter(self)
        self.stack.addWidget(self.list)
        self.empty = EmptyState()
        self.stack.addWidget(self.empty)
        holder_layout.addWidget(self.stack, 1)

        self.selection_bar = QFrame()
        self.selection_bar.setObjectName("SelectionBar")
        bar = QHBoxLayout(self.selection_bar)
        bar.setContentsMargins(16, 8, 8, 8)
        self.selection_label = QLabel()
        self.selection_label.setStyleSheet("font-weight: 700; background: transparent;")
        bar.addWidget(self.selection_label)
        bar.addStretch()
        self.install_checked_btn = button(tr("Установить"), "PrimaryButton", "download", tokens, "accent_text")
        self.install_checked_btn.clicked.connect(self._install_checked)
        clear = button(tr("Снять выделение"), "GhostButton", "x", tokens, "text_muted")
        clear.clicked.connect(self.clear_checked)
        bar.addWidget(self.install_checked_btn)
        bar.addWidget(clear)
        self.selection_bar.hide()
        holder_layout.addWidget(self.selection_bar)
        body.addWidget(holder, 1)

        self.details = CatalogDetails(self)
        self.details.closed.connect(self._close_details)
        self.details.hide()
        body.addWidget(self.details)
        root.addLayout(body, 1)
        self.status = label("", "Faint")
        root.addWidget(self.status)

    # --- Состояние ---

    def set_tokens(self, tokens: dict):
        self.tokens = tokens
        self.search.set_tokens(tokens)
        refresh_button_icons(self, tokens)
        style_segments(self.tab_group, tokens)
        self.details.set_tokens(tokens)
        self.list.viewport().update()

    def set_context(self, game_version: str, loader: str, installed: Dict[str, Set[str]]):
        changed = (game_version, loader) != (self.game_version, self.loader)
        self.game_version, self.loader, self.installed = game_version, loader, installed
        loader_title = {"fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge", "quilt": "Quilt"}.get(loader, loader)
        filters = f"Minecraft {game_version or '?'}" + (f" · {loader_title}" if self.content is MODS and loader else "")
        self.context_label.setText(tr('Моды, шейдеры и ресурспаки с Modrinth для текущей сборки: {0}. Обязательные зависимости ставятся автоматически.', filters))
        if changed and self.isVisible():
            self.refresh()
        self._apply_hidden()
        self.list.viewport().update()
        if self.details.isVisible() and self.details.hit:
            self.details.show_hit(self.details.hit)

    def hit_at(self, index) -> Optional[Dict]:
        return self.hits[index] if isinstance(index, int) and 0 <= index < len(self.hits) else None

    def current_hit(self) -> Optional[Dict]:
        item = self.list.currentItem()
        return self.hit_at(item.data(HIT_ROLE)) if item else None

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
            if installing:
                self.checked.pop(pid, None)
        self._refresh_selection()
        self.list.viewport().update()

    def icon_updated(self):
        self.list.viewport().update()

    # --- Отметки ---

    def toggle_checked(self, hit: Dict):
        pid = hit.get("project_id")
        if pid in self.checked:
            del self.checked[pid]
        elif self.install_state(hit) == "available":
            self.checked[pid] = hit
        self._refresh_selection()
        self.list.viewport().update()

    def clear_checked(self):
        self.checked.clear()
        self._refresh_selection()
        self.list.viewport().update()

    def _refresh_selection(self):
        count = len(self.checked)
        self.selection_bar.setVisible(count > 0)
        self.selection_label.setText(tr('Выбрано: {0}', count))
        self.install_checked_btn.setText(tr('Установить ({0})', count))

    def _install_checked(self):
        if self.checked:
            self.install_many_requested.emit(list(self.checked.values()))

    # --- Панель проекта ---

    def _on_current(self, current, previous):
        hit = self.hit_at(current.data(HIT_ROLE)) if current else None
        if hit:
            self.details.show_hit(hit)
            self.details.show()

    def _close_details(self):
        self.details.hide()
        self.list.setCurrentRow(-1)

    def show_similar(self, hit: Dict):
        cats = [c for c in hit.get("display_categories") or hit.get("categories") or [] if c not in LOADER_CATEGORIES]
        if cats:
            index = self.category_combo.findData(cats[0])
            if index >= 0:
                self.search.blockSignals(True)
                self.search.clear()
                self.search.blockSignals(False)
                self.category_combo.setCurrentIndex(index)
                return
        self.search.setText(hit.get("title") or "")

    def search_like(self, entry: Dict):
        """«Найти замену»: проекты тех же категорий, популярные сверху."""
        self.sort_combo.blockSignals(True)
        self.sort_combo.setCurrentIndex(max(0, self.sort_combo.findData("downloads")))
        self.sort_combo.blockSignals(False)
        cats = [c for c in entry.get("categories") or [] if c not in LOADER_CATEGORIES]
        self.search.blockSignals(True)
        self.search.setText("" if cats else (entry.get("name") or ""))
        self.search.blockSignals(False)
        index = self.category_combo.findData(cats[0]) if cats else -1
        self.category_combo.blockSignals(True)
        self.category_combo.setCurrentIndex(max(0, index))
        self.category_combo.blockSignals(False)
        self.refresh()

    def _context_menu(self, pos):
        item = self.list.itemAt(pos)
        hit = self.hit_at(item.data(HIT_ROLE)) if item else None
        if not hit:
            return
        t = self.tokens
        menu = QMenu(self)
        if self.install_state(hit) == "available":
            menu.addAction(make_action(menu, t, "download", tr("Установить"), lambda: self.install_requested.emit(hit), "accent"))
            checked = hit.get("project_id") in self.checked
            menu.addAction(make_action(menu, t, "check", tr("Снять отметку") if checked else tr("Отметить для установки"),
                                       lambda: self.toggle_checked(hit)))
        else:
            menu.addAction(make_action(menu, t, "package", tr("Показать в моих модах"),
                                       lambda: self.show_installed_requested.emit(hit)))
        menu.addAction(make_action(menu, t, "info", tr("Подробнее"), lambda: self.list.setCurrentItem(item)))
        menu.addAction(make_action(menu, t, "compass", tr("Похожие проекты"), lambda: self.show_similar(hit)))
        menu.addSeparator()
        menu.addAction(make_action(menu, t, "external", tr("Открыть на Modrinth"), lambda: webbrowser.open(hit_url(hit))))
        menu.addAction(make_action(menu, t, "copy", tr("Копировать ссылку"),
                                   lambda: QGuiApplication.clipboard().setText(hit_url(hit))))
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def eventFilter(self, obj, event):
        if obj is self.list and event.type() == QEvent.Type.KeyPress:
            hit = self.current_hit()
            if event.key() == Qt.Key.Key_Space and hit:
                self.toggle_checked(hit)
                return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and hit:
                self.details.show_hit(hit)
                self.details.show()
                return True
            if event.key() == Qt.Key.Key_Escape and self.details.isVisible():
                self._close_details()
                return True
        return super().eventFilter(obj, event)

    def _set_content(self, content: ContentType):
        self.content = content
        style_segments(self.tab_group, self.tokens)
        self.checked.clear()
        self._refresh_selection()
        self.details.hide()
        self._load_categories()
        self.set_context(self.game_version, self.loader, self.installed)
        self.refresh()

    def _load_categories(self):
        project_type = self.content.project_type
        self.run_task(get_categories, (project_type,), lambda cats: self._on_categories(project_type, cats),
                      lambda e: None, None)

    def _on_categories(self, project_type: str, categories: List[Dict]):
        if project_type != self.content.project_type:
            return
        current = self.category_combo.currentData()
        self.category_combo.blockSignals(True)
        self.category_combo.clear()
        self.category_combo.addItem(tr("Все категории"), None)
        names = sorted({c["name"] for c in categories if c.get("header") in ("categories", "features", "style", None)},
                       key=lambda n: CATEGORY_LABELS.get(n, n))
        for name in names:
            self.category_combo.addItem(CATEGORY_LABELS.get(name, name).capitalize(), name)
        self.category_combo.setCurrentIndex(max(0, self.category_combo.findData(current)))
        self.category_combo.blockSignals(False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.details.setFixedWidth(panel_width(self.width() - 56))

    def showEvent(self, event):
        super().showEvent(event)
        style_segments(self.tab_group, self.tokens)
        if self.category_combo.count() <= 1:
            self._load_categories()
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
        if value >= bar.maximum() - 200 and len(self.hits) < self.total and not self._loading:
            self._search(len(self.hits))

    def _search(self, offset: int):
        if not self.game_version and self.compatible.isChecked():
            self._show_empty("alert", tr("Не указана версия игры"), tr("Выберите сборку с известной версией Minecraft."))
            return
        request = self._request
        self._loading = True
        self.status.setText(tr("Ищем на Modrinth…"))
        compatible = self.compatible.isChecked()
        loaders = self.content.loaders(self.loader) if self.content is MODS and compatible else None
        category = self.category_combo.currentData()
        args = (self.search.text().strip(), self.content.project_type, self.game_version if compatible else None, loaders,
                self.sort_combo.currentData(), offset, PAGE_SIZE, [category] if category else None,
                self.side_combo.currentData() or None)
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
        self._apply_hidden()
        if not self.hits:
            self._show_empty("search", tr("Ничего не найдено"), tr("Попробуйте другой запрос или уберите фильтры."))
        else:
            self.stack.setCurrentIndex(0)
        self.status.setText(tr('Найдено: {0}  ·  показано {1}', format_count(self.total), len(self.hits))
                            + (tr("  ·  Пробел — отметить, Enter — подробнее") if self.hits else ""))
        # Если скрытые установленные «съели» всю страницу — подгружаем следующую
        if self.hide_installed.isChecked() and len(self.hits) < self.total and \
                all(self.list.item(i).isHidden() for i in range(self.list.count())):
            self._search(len(self.hits))

    def _apply_hidden(self):
        hide = self.hide_installed.isChecked()
        for i in range(self.list.count()):
            item = self.list.item(i)
            hit = self.hit_at(item.data(HIT_ROLE))
            item.setHidden(bool(hide and hit and self.install_state(hit) == "installed"))

    def _on_error(self, request: int, error: str):
        if request != self._request:
            return
        self._loading = False
        self.status.setText("")
        self._show_empty("error", tr("Не удалось выполнить поиск"), tr('Проверьте подключение к интернету.\n{0}', error))

    def _show_empty(self, icon_name: str, title: str, text: str):
        self.empty.set_content(self.tokens, icon_name, title, text)
        self.stack.setCurrentIndex(1)
