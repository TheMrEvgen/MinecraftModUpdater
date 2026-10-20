"""Диалоги новых возможностей: список изменений перед обновлением, переход на другую версию игры, ввод текста."""
import re
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QSplitter, QTextBrowser, QVBoxLayout, QWidget,
)

import icons
from content import MODS, ContentType
from enums import UpdateChannel
from ui.widgets import button, label
from ui_components import DialogTitleBar
from view_model import format_size
from i18n import tr

VERSION_TYPES = {"release": tr("релиз"), "beta": tr("бета"), "alpha": tr("альфа")}
ENTRY_ROLE = Qt.ItemDataRole.UserRole + 1


class FramelessDialog(QDialog):
    """Окно в стиле приложения: свой заголовок, отступы, ряд кнопок снизу."""

    def __init__(self, parent, title: str, min_width: int = 520):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setObjectName("DialogFrame")
        self.setMinimumWidth(min_width)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(DialogTitleBar(self))
        content = QWidget()
        self.body = QVBoxLayout(content)
        self.body.setContentsMargins(20, 16, 20, 18)
        self.body.setSpacing(12)
        outer.addWidget(content, 1)
        self.buttons = QHBoxLayout()
        self.buttons.addStretch()

    def finish_layout(self):
        self.body.addLayout(self.buttons)

    def add_button(self, text: str, primary: bool = False, slot: Optional[Callable] = None) -> QPushButton:
        btn = QPushButton(text)
        if primary:
            btn.setObjectName("PrimaryButton")
            btn.setDefault(True)
        btn.clicked.connect(slot or self.reject)
        self.buttons.addWidget(btn)
        return btn


def ask_text(parent, title: str, prompt: str, placeholder: str = "", text: str = "") -> Optional[str]:
    dialog = FramelessDialog(parent, title, 420)
    dialog.body.addWidget(QLabel(prompt))
    edit = QLineEdit(text)
    edit.setPlaceholderText(placeholder)
    dialog.body.addWidget(edit)
    dialog.add_button(tr("Отмена"))
    dialog.add_button(tr("Готово"), True, dialog.accept)
    dialog.finish_layout()
    edit.setFocus()
    if dialog.exec() and edit.text().strip():
        return edit.text().strip()
    return None


def demote_headings(markdown: str, levels: int = 3) -> str:
    """Авторы часто начинают описание с «# Заголовка» — в окне он огромный. Уменьшаем уровни заголовков."""
    return re.sub(r"^(#{1,6})(?=\s)", lambda m: "#" * min(6, len(m.group(1)) + levels), markdown or "", flags=re.MULTILINE)


_IMAGE_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_IMAGE_LINK_MD = re.compile(r"\[\s*!\[[^\]]*\]\([^)]*\)\s*\]\(([^)]*)\)")
_HTML_DROP = re.compile(r"<\s*(img|iframe|video|source|picture)\b[^>]*>(\s*</\s*\1\s*>)?", re.IGNORECASE)
_HTML_UNWRAP = re.compile(r"</?\s*(center|div|span|p|font|details|sub|sup)\b[^>]*>", re.IGNORECASE)
_SUMMARY = re.compile(r"<\s*summary\b[^>]*>(.*?)<\s*/\s*summary\s*>", re.IGNORECASE | re.DOTALL)


def prepare_markdown(markdown: str) -> str:
    """
    Описание с Modrinth для QTextBrowser: картинки он не загружает (вместо них — пустые значки), поэтому
    картинки убираем (картинка-ссылка становится обычной ссылкой), обёртки HTML снимаем, заголовки уменьшаем.
    """
    text = markdown or ""
    text = _IMAGE_LINK_MD.sub(lambda m: tr('[ссылка]({0})', m.group(1)), text)
    text = _IMAGE_MD.sub("", text)
    text = _HTML_DROP.sub("", text)
    text = _SUMMARY.sub(lambda m: f"\n\n**{m.group(1).strip()}**\n\n", text)
    text = _HTML_UNWRAP.sub("", text)
    text = re.sub(r"<\s*br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return demote_headings(text.strip())


def changelog_markdown(versions: List[Dict], fallback: str = "") -> str:
    """Списки изменений нескольких версий одним документом (новые сверху)."""
    if not versions:
        return prepare_markdown(fallback) or tr("_Автор не оставил описания изменений._")
    parts = []
    for v in versions:
        kind = VERSION_TYPES.get(v.get("version_type"), v.get("version_type") or "")
        parts.append(f"### {v.get('version_number')}  ·  {kind}  ·  {v.get('date')}\n\n"
                     f"{prepare_markdown(v.get('changelog') or '') or tr('_Без описания._')}")
    return "\n\n---\n\n".join(parts)


class UpdateReviewDialog(FramelessDialog):
    """
    «Что нового» перед массовым обновлением: список модов с отметками, справа — изменения выбранного мода
    за все версии между установленной и новой. Возвращает отмеченные записи (selected_entries).
    """

    def __init__(self, parent, tokens: dict, entries: List[Dict], icon_provider: Callable[[Dict], Optional[QPixmap]],
                 changelog_loader: Callable[[Callable[[Dict], None]], None], verb: str = tr("Обновить")):
        super().__init__(parent, tr("Что нового"), 900)
        self.resize(980, 640)
        self.tokens = tokens
        self.entries = entries
        self.verb = verb
        self.changelogs: Optional[Dict[str, List[Dict]]] = None

        intro = label(tr('Будет обновлено модов: {0}. Снимите отметку с тех, которые обновлять не нужно — закрепите их версию в меню мода, чтобы они не предлагались снова.', len(entries)), "Muted", wrap=True)
        self.body.addWidget(intro)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        self.list = QListWidget()
        self.list.setObjectName("ReviewList")
        self.list.setIconSize(QSize(28, 28))
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        for entry in entries:
            current, new = entry.get("current_version") or "", entry.get("new_version") or ""
            text = f"{entry.get('name')}\n{current} → {new}" if current else f"{entry.get('name')}\n{new}"
            item = QListWidgetItem(text)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(ENTRY_ROLE, len(self.list))  # индекс в entries (dict через QVariant копируется)
            pm = icon_provider(entry)
            item.setIcon(QIcon(pm) if pm and not pm.isNull() else icons.icon("package", tokens["text_faint"], 28))
            item.setSizeHint(QSize(0, 48))
            self.list.addItem(item)
        self.list.setMinimumWidth(320)
        splitter.addWidget(self.list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 0, 0, 0)
        self.title = label("", "SectionTitle")
        self.subtitle = label("", "Faint")
        self.changelog = QTextBrowser()
        self.changelog.setOpenExternalLinks(True)
        right_layout.addWidget(self.title)
        right_layout.addWidget(self.subtitle)
        right_layout.addWidget(self.changelog, 1)
        splitter.addWidget(right)
        splitter.setSizes([340, 600])
        self.body.addWidget(splitter, 1)

        self.summary = label("", "Muted")
        self.buttons.insertWidget(0, self.summary)
        self.add_button(tr("Отмена"))
        self.ok = self.add_button(verb, True, self.accept)
        self.finish_layout()

        self.list.currentRowChanged.connect(self._show_entry)
        self.list.itemChanged.connect(lambda _: self._update_summary())
        self.list.setCurrentRow(0)
        self._update_summary()
        changelog_loader(self._on_changelogs)

    def _on_changelogs(self, changelogs: Dict[str, List[Dict]]):
        self.changelogs = changelogs or {}
        self._show_entry(self.list.currentRow())

    def _show_entry(self, row: int):
        if not 0 <= row < len(self.entries):
            return
        entry = self.entries[row]
        self.title.setText(entry.get("name", ""))
        current = entry.get("current_version") or tr("не установлен")
        size = f"  ·  {format_size(entry['new_size'])}" if entry.get("new_size") else ""
        self.subtitle.setText(f"{current}  →  {entry.get('new_version') or ''}{size}")
        latest = (entry.get("_latest_info") or {}).get("changelog") or ""
        if self.changelogs is None:
            text = (prepare_markdown(latest) + tr("\n\n_Загружаем изменения промежуточных версий…_")) if latest \
                else tr("_Загружаем список изменений…_")
        else:
            text = changelog_markdown(self.changelogs.get(entry.get("project_id"), []), latest)
        self.changelog.setMarkdown(text[:60000])

    def selected_entries(self) -> List[Dict]:
        return [self.entries[self.list.item(i).data(ENTRY_ROLE)] for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]

    def _update_summary(self):
        selected = self.selected_entries()
        size = sum(e.get("new_size") or 0 for e in selected)
        self.summary.setText(tr('Выбрано: {0} из {1}', len(selected), len(self.entries)) + (f"  ·  {format_size(size)}" if size else ""))
        self.ok.setText(f"{self.verb} ({len(selected)})")
        self.ok.setEnabled(bool(selected))


class MigrationDialog(FramelessDialog):
    """
    Переход сборки на другую версию Minecraft: проверка, какие моды уже вышли под неё,
    и (после подтверждения) обновление всех модов, у которых версия есть.
    """

    def __init__(self, parent, tokens: dict, entries: List[Dict], game_version: str, loader: str,
                 channel: UpdateChannel, run_task: Callable, content: ContentType = MODS, target: Optional[str] = None):
        super().__init__(parent, tr("Переход на другую версию Minecraft"), 640)
        self._preselect = target
        self.resize(700, 600)
        self.tokens = tokens
        self.entries = entries
        self.loader = loader
        self.channel = channel
        self.content = content
        self.run_task = run_task
        self.results: List[Dict] = []
        self.target = ""

        self.body.addWidget(label(tr('Сейчас: Minecraft {0} ({1}). Выберите версию — программа проверит, для каких модов уже есть совместимые версии.', game_version, loader), "Muted", wrap=True))
        row = QHBoxLayout()
        self.version_combo = QComboBox()
        self.version_combo.setEditable(True)
        self.version_combo.setMinimumWidth(180)
        self.version_combo.setPlaceholderText(tr("Загрузка версий…"))
        self.check_btn = button(tr("Проверить"), None, "refresh", tokens, "text")
        self.check_btn.clicked.connect(self._check)
        row.addWidget(QLabel(tr("Версия игры:")))
        row.addWidget(self.version_combo)
        row.addWidget(self.check_btn)
        row.addStretch()
        self.body.addLayout(row)

        self.summary = label("", "SectionTitle")
        self.summary.hide()
        self.body.addWidget(self.summary)
        self.list = QListWidget()
        self.list.setObjectName("ReviewList")
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list.setMinimumHeight(300)
        self.body.addWidget(self.list, 1)
        self.disable_missing = QCheckBox(tr("Выключить моды, для которых версии нет (их можно включить позже)"))
        self.disable_missing.setChecked(True)
        self.disable_missing.hide()
        self.body.addWidget(self.disable_missing)
        self.note = label(tr("После перехода смените версию игры и загрузчика в лаунчере — программа обновляет только моды."),
                          "Faint", wrap=True)
        self.body.addWidget(self.note)

        self.add_button(tr("Отмена"))
        self.ok = self.add_button(tr("Перейти"), True, self.accept)
        self.ok.setEnabled(False)
        self.finish_layout()

        from mod_updater_logic import get_game_versions
        self.run_task(get_game_versions, (), self._on_versions,
                      lambda e: self.version_combo.setPlaceholderText(tr("Введите версию")), tr("Загружаем версии игры…"))
        self._current = game_version

    def _on_versions(self, versions: List[str]):
        self.version_combo.clear()
        self.version_combo.addItems(versions)
        if self._preselect and self._preselect in versions:  # Открыто из «Вышла новая версия» — сразу проверяем
            self.version_combo.setCurrentText(self._preselect)
            self._check()
        elif versions and versions[0] != self._current:
            self.version_combo.setCurrentIndex(0)
        elif len(versions) > 1:
            self.version_combo.setCurrentIndex(1)

    def _check(self):
        target = self.version_combo.currentText().strip()
        if not target:
            return
        from scanner import check_target_version
        self.check_btn.setEnabled(False)
        self.check_btn.setText(tr("Проверяем…"))
        installed = [e for e in self.entries if e.get("filepath")]
        self.run_task(check_target_version, (installed, target, self.loader, self.channel, self.content),
                      lambda results: self._on_checked(target, results), self._on_failed,
                      tr('Проверяем совместимость с {0}…', target))

    def _on_failed(self, error: str):
        self.check_btn.setEnabled(True)
        self.check_btn.setText(tr("Проверить"))
        self.summary.setText(tr('Не удалось проверить: {0}', error))
        self.summary.show()

    def _on_checked(self, target: str, results: List[Dict]):
        t = self.tokens
        self.check_btn.setEnabled(True)
        self.check_btn.setText(tr("Проверить"))
        self.target, self.results = target, results
        ready = [r for r in results if r["version"]]
        missing = [r for r in results if not r["version"]]
        self.summary.setText(tr('Готовы к {0}: {1} из {2}', target, len(ready), len(results)))
        self.summary.setStyleSheet(f"color: {t['accent'] if not missing else t['warning']};")
        self.summary.show()
        self.list.clear()
        for r in sorted(results, key=lambda r: (bool(r["version"]), str(r["entry"].get("name")).lower())):
            entry, version = r["entry"], r["version"]
            if version and version.get("id") == (entry.get("installed_info") or {}).get("id"):
                item = QListWidgetItem(icons.icon("check_circle", t["accent"], 16),
                                       tr('{0}   {1} — уже подходит', entry.get('name'), entry.get('current_version') or ''))
            elif version:
                text = f"{entry.get('name')}   {entry.get('current_version') or ''} → {version.get('version_number')}"
                item = QListWidgetItem(icons.icon("check_circle", t["accent"], 16), text)
            else:
                reason = tr("нет на Modrinth") if not entry.get("project_id") else tr('нет версии под {0}', target)
                item = QListWidgetItem(icons.icon("error", t["danger"], 16), f"{entry.get('name')}   — {reason}")
            self.list.addItem(item)
        self.disable_missing.setText(tr('Выключить моды без версии под {0} ({1}) — их можно включить позже', target, len(missing)))
        self.disable_missing.setVisible(bool(missing))
        self.ok.setText(tr('Перейти на {0}', target))
        self.ok.setEnabled(bool(ready) and target != self._current)
