"""Окно «Моды во всех сборках»: поиск по всем сборкам и (по желанию) экономия места жёсткими ссылками."""
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QHeaderView, QTreeWidget, QTreeWidgetItem, QVBoxLayout

import all_packs
from i18n import plural, tr
from ui.dialogs import FramelessDialog
from ui.widgets import SearchBox, button, label
from ui_components import confirm
from view_model import format_size


class AllPacksDialog(FramelessDialog):
    def __init__(self, parent, tokens: dict, profiles: Dict[str, Dict], cache_file: str, run_task: Callable,
                 on_changed: Optional[Callable[[], None]] = None):
        super().__init__(parent, tr("Моды во всех сборках"), 760)
        self.setMinimumHeight(600)
        self.tokens = tokens
        self.run_task = run_task
        self.on_changed = on_changed
        self.profiles = profiles
        self.cache_file = cache_file
        self.rows: List[Dict] = []
        self.groups: List[List[Dict]] = []

        self.search = SearchBox(tokens, tr("Мод во всех сборках: название или файл…"))
        self.search.setMinimumHeight(36)
        self._timer = QTimer(self, singleShot=True, interval=200)
        self._timer.timeout.connect(self._fill)
        self.search.textChanged.connect(lambda: self._timer.start())
        self.body.addWidget(self.search)
        self.summary = label(tr("Читаем сборки…"), "Faint")
        self.body.addWidget(self.summary)
        self.tree = QTreeWidget()
        self.tree.setObjectName("ModList")
        self.tree.setHeaderLabels([tr("Мод / сборка"), tr("Версия"), tr("Размер")])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.body.addWidget(self.tree, 1)

        # Место на диске — только по кнопке
        self.space_card = QFrame()
        self.space_card.setObjectName("Card")
        card = QVBoxLayout(self.space_card)
        card.setContentsMargins(14, 12, 14, 12)
        card.setSpacing(6)
        self.space_title = label("", "SectionTitle", wrap=True)
        self.space_text = label(tr("Одинаковые файлы модов можно хранить на диске один раз (жёсткие ссылки): файлы "
                                   "останутся на своих местах, моды и сборки не изменятся, а обновление в одной сборке "
                                   "не затронет другие."), "Faint", wrap=True)
        card.addWidget(self.space_title)
        card.addWidget(self.space_text)
        row = QHBoxLayout()
        row.addStretch()
        self.link_btn = button(tr("Освободить место…"), None, "layers", tokens, "text")
        self.link_btn.clicked.connect(self._link)
        row.addWidget(self.link_btn)
        card.addLayout(row)
        self.space_card.hide()
        self.body.addWidget(self.space_card)

        self.add_button(tr("Закрыть"), True, self.accept)
        self.finish_layout()
        self._load()

    def _load(self):
        self.run_task(all_packs.collect, (self.profiles, self.cache_file), self._on_loaded,
                      lambda e: self.summary.setText(tr("Не удалось прочитать сборки: {0}", e)), None)

    def _on_loaded(self, rows: List[Dict]):
        self.rows = rows
        self.groups = all_packs.duplicate_groups(rows)
        packs = len({r["pack"] for r in rows})
        self.summary.setText(tr("Сборок: {0} · модов: {1}", packs, len(rows)))
        saving = all_packs.savings(self.groups)
        self.space_card.setVisible(bool(self.groups))
        files = sum(len({r["inode"] for r in g}) - 1 for g in self.groups)
        self.space_title.setText(tr("Одинаковые файлы в разных сборках: {0} {1} — можно освободить {2}",
                                    files, plural(files, tr("файл"), tr("файла"), tr("файлов")), format_size(saving)))
        self._fill()

    def _fill(self):
        self.tree.clear()
        result = all_packs.search(self.rows, self.search.text())
        for name, items in result[:300]:
            packs = sorted({r["pack"] for r in items})
            versions = sorted({r["version"] for r in items if r["version"]})
            top = QTreeWidgetItem([tr("{0}   ·   {1} {2}", name, len(packs), plural(len(packs), tr("сборка"), tr("сборки"), tr("сборок"))),
                                   versions[0] if len(versions) == 1 else tr("разные"), ""])
            for row in sorted(items, key=lambda r: r["pack"].lower()):
                child = QTreeWidgetItem([row["pack"] + (tr(" (выключен)") if row.get("disabled") else ""), row["version"],
                                         format_size(row["size"])])
                child.setToolTip(0, row["path"])
                top.addChild(child)
            self.tree.addTopLevelItem(top)
            top.setExpanded(bool(self.search.text().strip()) or len(result) <= 5)
        if self.search.text().strip() and not result:
            self.tree.addTopLevelItem(QTreeWidgetItem([tr("Ни в одной сборке такого мода нет"), "", ""]))

    def _link(self):
        saving = format_size(all_packs.savings(self.groups))
        if not confirm(self, tr("Освободить место"),
                       tr("Хранить одинаковые файлы модов на диске один раз и освободить {0}?\n\nФайлы останутся в своих "
                          "папках, сборки и моды не изменятся. Если игра какой-то сборки запущена, её файлы пропустим.",
                          saving), tr("Освободить"), tr("Отмена")):
            return
        self.link_btn.setEnabled(False)
        self.run_task(all_packs.link_all, (self.groups,), self._on_linked,
                      lambda e: self.space_title.setText(tr("Не удалось: {0}", e)), None)

    def _on_linked(self, result):
        linked, freed, errors = result
        self.space_title.setText(tr("Освобождено {0}: объединено файлов — {1}.", format_size(freed), linked)
                                 + (tr(" Пропущено: {0}.", len(errors)) if errors else ""))
        self.space_text.setText("\n".join(errors[:5]) if errors else
                                tr("Готово. Если позже обновить мод в одной сборке, у неё появится свой новый файл."))
        self.groups = []
        self.link_btn.hide()
        if self.on_changed:
            self.on_changed()
