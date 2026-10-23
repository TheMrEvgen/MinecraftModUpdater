"""Список модов: данные, фильтры, выделение, панель мода, вид (плоский/дерево), меню строки.

Примесь к главному окну (ModUpdaterApp): методы работают с его атрибутами."""
import os
from typing import Iterator, List, Optional

from PyQt6.QtCore import QEasingCurve, QItemSelectionModel, QVariantAnimation, Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QMenu

import icons
from i18n import tr
from ui.mod_list import entry_at, meta_at
from ui.widgets import button, make_action
from view_model import (
    CAT_ALL, CAT_DISABLED, CAT_INSTALL, CAT_NOT_FOUND, CAT_PROBLEMS, CAT_UPDATES, VIEW_FLAT, VIEW_MODES, VIEW_TREE,
    build_rows, category_counts, collect_tags, entry_key, format_size, is_updatable, iter_all_entries, pin_key,
    status_of,
)
from window.common import ANIMATION_MS, BULK_STATUSES, PAGE_MODS


class ModListMixin:
    # ================================================================== Данные списка

    def _all_entries(self) -> Iterator[dict]:
        seen = set()
        for entry in list(iter_all_entries(self.mods_data)) + self.model.items():
            if id(entry) not in seen:
                seen.add(id(entry))
                yield entry

    def _set_data(self, mods_data: List[dict], keep_selection: bool = True):
        """Обновляет список модов, сохраняя выделение и открытую панель деталей."""
        selected_key = entry_key(self.details.entry) if keep_selection and self.details.entry else None
        self.mods_data = mods_data
        self._apply_view_settings()
        pinned = self._profile().get("pinned") or {}
        for entry in iter_all_entries(mods_data):
            entry["_pinned"] = bool(entry.get("filepath")) and pin_key(entry) in pinned
        self._apply_personal_flags(mods_data)
        rows, self._count_items = build_rows(mods_data, self._view_mode())
        self.model.set_rows(rows)
        items = self.model.items()
        self.proxy.sort(0)
        self._refresh_counts()
        self._refresh_selection_ui()
        self._refresh_empty_state()
        self._refresh_actions()
        self._refresh_presets()
        if selected_key is not None:
            match = next((e for e in items if entry_key(e) == selected_key), None)
            if match:
                self._select_entry(match)
            else:
                self._close_details()

    def _refresh_counts(self):
        counts = category_counts(self._count_items)
        for category, card in self.cards.items():
            card.set_count(counts.get(category, 0))
        if self.is_import_mode:
            visible = {CAT_ALL, CAT_INSTALL, CAT_PROBLEMS}
        else:
            visible = {CAT_ALL, CAT_UPDATES, CAT_PROBLEMS, CAT_NOT_FOUND}
            if counts.get(CAT_INSTALL):
                visible.add(CAT_INSTALL)
            if counts.get(CAT_DISABLED):
                visible.add(CAT_DISABLED)
        installed = [e for e in self._count_items if e.get("filepath")]
        total = sum(e.get("size") or 0 for e in installed)
        self.size_label.setText(tr('{0} файлов  ·  {1}', len(installed), format_size(total)) if installed else "")
        self._refresh_tag_combo()
        for category, card in self.cards.items():
            card.setVisible(category in visible)
        if not self.cards[self.proxy.category].isVisible():
            self._set_category(CAT_ALL)

    def _set_category(self, category: str):
        self.cards[category].setChecked(True)
        self._apply_filter(category=category)

    def _apply_filter(self, category: Optional[str] = None, query: Optional[str] = None):
        self.proxy.set_filter(category, query)
        self._refresh_empty_state()
        self._refresh_selection_ui()

    def _refresh_empty_state(self):
        t = self.tokens
        profile = self._profile()
        game_dir = profile.get("mods_path", "")
        has_items = bool(self.model.items())
        self.cards_widget.setVisible(has_items and not self._minimal)
        self.toolbar_widget.setVisible(has_items)
        if self.proxy.rowCount() > 0:
            self.list_stack.setCurrentIndex(0)
            return
        if not game_dir or not os.path.isdir(game_dir):
            b1 = button(tr("Указать папку игры"), "PrimaryButton", "folder", t, "accent_text")
            b1.clicked.connect(self._edit_current_profile)
            b2 = button(tr("Найти в лаунчерах"), None, "box_search", t, "text")
            b2.clicked.connect(self._discover_now)
            self.empty_state.set_content(t, "folder", tr("Папка игры не найдена"),
                                         tr("Укажите папку игры, в которой лежит папка mods, или найдите сборки "
                                         "в Prism Launcher, Modrinth App и CurseForge."), [b1, b2])
        elif self._busy in ("local", "import") and not self.model.items():
            self.empty_state.set_content(t, "refresh", tr("Загрузка…"), tr("Читаем список модов."))
        elif not self.model.items():
            b1 = button(tr("Импортировать сборку"), None, "import", t, "text")
            b1.clicked.connect(self.import_mod_list)
            b2 = button(tr("Открыть .mrpack"), None, "package", t, "text")
            b2.clicked.connect(self.import_mrpack_dialog)
            self.empty_state.set_content(t, "drop", tr("Модов пока нет"),
                                         tr("Перетащите сюда .jar-файлы или .mrpack, вставьте строку сборки "
                                         "или импортируйте список от друга."), [b1, b2])
        else:
            b1 = button(tr("Сбросить фильтры"), None, "x", t, "text")
            b1.clicked.connect(self._reset_filters)
            self.empty_state.set_content(t, "search", tr("Ничего не найдено"),
                                         tr("Попробуйте изменить запрос или выбрать другой фильтр."), [b1])
        self.list_stack.setCurrentIndex(1)

    def _reset_filters(self):
        self.search.clear()
        self.tag_combo.setCurrentIndex(0)
        self._set_category(CAT_ALL)

    def _refresh_actions(self):
        busy = self._busy is not None
        bulk = self._bulk_candidates()
        verb = tr("Установить все") if self.is_import_mode else tr("Обновить все")
        self.update_all_btn.setText(f"{verb} ({len(bulk)})" if bulk else verb)
        self.update_all_btn.setEnabled(bool(bulk) and not busy)
        scanning = self._busy in ("scan", "local", "import")
        self.check_btn.setText(tr("Отмена") if scanning else tr("Проверить"))
        self.check_btn.setProperty("icon_name", "x" if scanning else "refresh")
        self.check_btn.setIcon(icons.icon("x" if scanning else "refresh", self.tokens["text"], 16))
        self.check_btn.setEnabled((scanning or not busy) and bool(self.mods_path) and not self.is_import_mode)
        if self._game_running:
            self.update_all_btn.setEnabled(False)
            self.update_all_btn.setToolTip(tr("Закройте игру, чтобы обновлять моды"))
        else:
            self.update_all_btn.setToolTip("")
        self._refresh_play_button()
        self.more_btn.setEnabled(not busy)
        self.edit_btn.setEnabled(not busy)
        self.sidebar.add_btn.setEnabled(not busy)
        self.selection_bar.update_btn.setEnabled(not busy and self.selection_bar.update_btn.isEnabled())

    def _bulk_candidates(self) -> List[dict]:
        return [e for e in self.model.items() if is_updatable(e) and status_of(e) in BULK_STATUSES]

    def _refresh_tag_combo(self):
        current = self.tag_combo.currentData()
        tags = collect_tags(self._count_items, self.mod_tags)
        self.tag_combo.blockSignals(True)
        self.tag_combo.clear()
        self.tag_combo.addItem(tr("Все метки"), None)
        for key, title, count in tags:
            self.tag_combo.addItem(f"{title}  ({count})", key)
        index = self.tag_combo.findData(current)
        self.tag_combo.setCurrentIndex(max(0, index))
        self.tag_combo.blockSignals(False)
        self.tag_combo.setVisible(len(tags) > 0)
        if current and index < 0:
            self._on_tag_selected()

    def _on_tag_selected(self, *_):
        self.proxy.set_tag(self.tag_combo.currentData())
        self.proxy.sort(0)
        self._refresh_empty_state()
        self._refresh_selection_ui()

    def _on_sort_changed(self, *_):
        self.proxy.set_sort_mode(self.sort_combo.currentData())
        if self.settings.get("per_profile_view", True):
            self._profile()["sort"] = self.sort_combo.currentData()
        self.view.relayout()

    def _restore_profile_view(self):
        """Вид и сортировка, сохранённые для этой сборки."""
        if not self.settings.get("per_profile_view", True):
            return
        sort = self._profile().get("sort")
        index = self.sort_combo.findData(sort) if sort else 0
        if index >= 0 and index != self.sort_combo.currentIndex():
            self.sort_combo.blockSignals(True)
            self.sort_combo.setCurrentIndex(index)
            self.sort_combo.blockSignals(False)
            self.proxy.set_sort_mode(self.sort_combo.currentData())

    def _icon_for_entry(self, entry: dict) -> Optional[QPixmap]:
        pid = entry.get("project_id")
        if not pid:
            return None
        pm = self._icon_pixmaps.get(pid)
        if pm is None:
            path = self.icon_loader.request(pid, entry.get("icon_url"))
            if path:
                pm = QPixmap(path)
                self._icon_pixmaps[pid] = pm
        return pm

    def _on_icon_ready(self, project_id: str, path: str):
        self._icon_pixmaps[project_id] = QPixmap(path)
        self.model.refresh_where(lambda e: e.get("project_id") == project_id)
        if self.details.entry and self.details.entry.get("project_id") == project_id:
            self.details.set_entry(self.details.entry, self._icon_pixmaps[project_id])

    def _refresh_project(self, project_id: Optional[str]):
        if project_id:
            self.model.refresh_where(lambda e: e.get("project_id") == project_id)
        else:
            self.model.refresh_all()

    # ================================================================== Выделение и панель деталей

    def _visible_entries(self) -> List[dict]:
        return [entry_at(self.proxy.index(r, 0)) for r in range(self.proxy.rowCount())]

    def _checked_entries(self) -> List[dict]:
        return [e for e in self.model.items() if e.get("_is_checked")]

    def _refresh_selection_ui(self):
        checked = self._checked_entries()
        visible = self._visible_entries()
        visible_checked = sum(1 for e in visible if e.get("_is_checked"))
        self.select_all.blockSignals(True)
        state = (Qt.CheckState.Unchecked if visible_checked == 0 else
                 Qt.CheckState.Checked if visible_checked == len(visible) else Qt.CheckState.PartiallyChecked)
        self.select_all.setCheckState(state)
        self.select_all.setEnabled(bool(visible))
        self.select_all.blockSignals(False)
        if checked:
            files = [e for e in checked if e.get("filepath")]
            self.selection_bar.set_state(len(checked), sum(1 for e in checked if e.get("download_url")),
                                         len(files), self.is_import_mode,
                                         all_disabled=bool(files) and all(e.get("disabled") for e in files))
            self.selection_bar.update_btn.setEnabled(self._busy is None and any(e.get("download_url") for e in checked))
            self.selection_bar.show()
            self.selection_bar.raise_()
            self._position_selection_bar()
        else:
            self.selection_bar.hide()

    def _position_selection_bar(self):
        bar = self.selection_bar
        bar.adjustSize()
        holder = self.list_holder.rect()
        bar.move((holder.width() - bar.width()) // 2, holder.height() - bar.height() - 16)

    def _on_select_all_clicked(self):
        visible = self._visible_entries()
        target = not all(e.get("_is_checked") for e in visible)
        for entry in visible:
            entry["_is_checked"] = target
        self.view.viewport().update()
        self._refresh_selection_ui()

    def _set_all_checked(self, value: bool):
        for entry in self.model.items():
            entry["_is_checked"] = value
        self.view.viewport().update()
        self._refresh_selection_ui()

    def _toggle_current(self):
        entry = entry_at(self.view.currentIndex())
        if entry:
            entry["_is_checked"] = not entry.get("_is_checked")
            self.view.viewport().update()
            self._refresh_selection_ui()

    def _on_current_changed(self, current, previous):
        entry = entry_at(current)
        if not entry:
            self.details.hide()
        elif self.settings.get("details_on_select", True) or self.details.isVisible():
            self.details.set_entry(entry, self._icon_for_entry(entry))
            self.details.show()

    def _open_current_details(self):
        entry = entry_at(self.view.currentIndex())
        if entry:
            self.details.set_entry(entry, self._icon_for_entry(entry))
            self.details.show()

    # ================================================================== Вид списка

    def _view_mode(self) -> str:
        mode = self.settings.get("view_mode", VIEW_FLAT)
        if self.settings.get("per_profile_view", True):
            mode = self._profile().get("view_mode") or mode
        return mode if mode in VIEW_MODES else VIEW_FLAT

    def _apply_view_settings(self):
        """Переносит настройки вида в делегат и фильтр (без пересборки строк)."""
        tree = self._view_mode() == VIEW_TREE
        self.delegate.tree = tree
        self.delegate.compact = bool(self.settings.get("compact_rows", False))
        self.delegate.groups = bool(self.settings.get("group_by_status", True))
        self.delegate.columns = set(self.settings.get("columns") or ())
        self.delegate.abandoned_hint = bool(self.settings.get("abandoned_warning", True))
        self.details.show_abandoned = self.delegate.abandoned_hint
        # Умолчание раскрытия применяем только при смене настройки, чтобы не сбрасывать «Раскрыть все» из меню строки
        expand = bool(self.settings.get("expand_dependencies", False))
        if getattr(self, "_expand_setting", None) != expand:
            self._expand_setting = expand
            self.proxy.set_expand_all(expand)

    def _rebuild_view(self):
        """Перестраивает список под новые настройки вида, сохраняя открытый мод."""
        self._set_data(self.mods_data)
        self.view.relayout()  # Высота строк могла измениться (компактный режим, заголовки групп)
        self.view.viewport().update()

    def _expand_all(self, expanded: bool):
        self.proxy.set_expand_all(expanded)
        self._after_tree_change()

    def _set_expanded(self, entry: dict, expanded: bool):
        if not self.settings.get("animations", True) or not self.isVisible():
            self.proxy.set_expanded(entry, expanded)
            self._after_tree_change()
            return
        # Плавное раскрытие: зависимости появляются с нулевой высотой и «вырастают» (сворачивание — наоборот)
        key = entry_key(entry)
        previous = self._animations.pop(key, None)
        if previous:
            previous.stop()
        start = self.delegate.anim.get(key, 0.0 if expanded else 1.0)
        if expanded:
            self.delegate.anim[key] = start
            self.proxy.set_expanded(entry, True)
            self._after_tree_change()
        animation = QVariantAnimation(self)
        animation.setDuration(ANIMATION_MS)
        animation.setStartValue(float(start))
        animation.setEndValue(1.0 if expanded else 0.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic if expanded else QEasingCurve.Type.InCubic)

        def step(value):
            self.delegate.anim[key] = float(value)
            self.view.relayout()

        def done():
            self._animations.pop(key, None)
            self.delegate.anim.pop(key, None)
            if not expanded:
                self.proxy.set_expanded(entry, False)
                self._after_tree_change()
            self.view.relayout()

        animation.valueChanged.connect(step)
        animation.finished.connect(done)
        self._animations[key] = animation
        animation.start()

    def _expand_current(self, expand: bool):
        index = self.view.currentIndex()
        entry, meta = entry_at(index), meta_at(index)
        if not entry or self._view_mode() != VIEW_TREE:
            return
        if meta.get("children"):
            if self.proxy.is_expanded(entry) != expand:
                self._set_expanded(entry, expand)
        elif meta.get("depth") and not expand:
            # ← на зависимости — к её моду
            parent = self.model.entry(meta["parent_row"])
            parent_index = self.proxy.mapFromSource(self.model.index(meta["parent_row"]))
            if parent and parent_index.isValid():
                self.view.setCurrentIndex(parent_index)

    def _after_tree_change(self):
        current = self.view.currentIndex()
        self.proxy.sort(0)
        self._refresh_empty_state()
        self._refresh_selection_ui()
        if current.isValid():
            self.view.scrollTo(self.view.currentIndex())

    def _select_entry(self, entry: dict):
        key = entry_key(entry)
        for row in range(self.proxy.rowCount()):
            idx = self.proxy.index(row, 0)
            if entry_key(entry_at(idx)) == key:
                self.view.selectionModel().setCurrentIndex(idx, QItemSelectionModel.SelectionFlag.ClearAndSelect)
                self.view.scrollTo(idx)
                return
        # Зависимость внутри свёрнутого мода — раскрываем его
        for row in range(self.model.rowCount()):
            meta = self.model.meta(row)
            if meta.get("depth") and entry_key(self.model.entry(row)) == key:
                parent = self.model.entry(meta["parent_row"])
                if not self.proxy.is_expanded(parent):
                    self._set_expanded(parent, True)
                    self._select_entry(entry)
                    return
        # Мод скрыт фильтром — показываем все
        if self.proxy.category != CAT_ALL or self.proxy.query or self.proxy.tag:
            self._reset_filters()
            self._select_entry(entry)

    def _close_details(self):
        self.view.selectionModel().clearSelection()
        self.view.setCurrentIndex(self.proxy.index(-1, 0))
        self.details.entry = None
        self.details.hide()

    def _focus_search(self):
        self.sidebar.set_page(PAGE_MODS)
        self.stack.setCurrentIndex(PAGE_MODS)
        self.search.setFocus()
        self.search.selectAll()

    def _on_escape(self):
        if self.search.text():
            self.search.clear()
        elif self.details.isVisible():
            self._close_details()
        elif self._checked_entries():
            self._set_all_checked(False)

    def _show_entry_menu(self, entry: dict, pos):
        t = self.tokens
        menu = QMenu(self)
        if not self.details.isVisible() or self.details.entry is not entry:
            menu.addAction(make_action(menu, t, "info", tr("Подробнее"),
                                       lambda: (self._select_entry(entry), self._open_current_details())))
        if entry.get("download_url"):
            verb = tr("Обновить до") if entry.get("filepath") else tr("Установить")
            menu.addAction(make_action(menu, t, "download", f"{verb} {entry.get('new_version', '')}",
                                       lambda: self.start_update([entry]), "accent"))
        if entry.get("project_id"):
            menu.addAction(make_action(menu, t, "history", tr("Выбрать другую версию…"),
                                       lambda: (self._select_entry(entry), self._load_versions(entry))))
            menu.addAction(make_action(menu, t, "external", tr("Открыть на Modrinth"), lambda: self._open_mod_page(entry)))
        if entry.get("filepath") and not self.is_import_mode:
            menu.addSeparator()
            pinned = bool(entry.get("_pinned"))
            menu.addAction(make_action(menu, t, "unlock" if pinned else "lock",
                                       tr("Разрешить обновления") if pinned else tr("Не обновлять этот мод (закрепить)"),
                                       lambda: self._toggle_pin(entry)))
            menu.addAction(make_action(menu, t, "power", tr("Включить") if entry.get("disabled") else tr("Выключить (без удаления)"),
                                       lambda: self._toggle_disabled([entry])))
        if entry.get("project_id") or entry.get("filepath"):
            menu.addAction(make_action(menu, t, "star_filled" if entry.get("_favorite") else "star",
                                       tr("Убрать из избранного") if entry.get("_favorite") else tr("В избранное"),
                                       lambda: self._toggle_favorite([entry]), "warning" if entry.get("_favorite") else "text_muted"))
            menu.addAction(make_action(menu, t, "pencil", tr("Заметка…"),
                                       lambda: (self._select_entry(entry), self._edit_note_current())))
            menu.addAction(make_action(menu, t, "tag", tr("Метки…"), lambda: self._show_tags_menu(entry, pos)))
        if self._abandoned(entry):
            menu.addAction(make_action(menu, t, "compass", tr("Найти замену в каталоге"), lambda: self._find_replacement(entry)))
        if self._view_mode() == VIEW_TREE:
            menu.addSeparator()
            menu.addAction(make_action(menu, t, "chevron_down", tr("Раскрыть все зависимости"), lambda: self._expand_all(True)))
            menu.addAction(make_action(menu, t, "chevron_right", tr("Свернуть все"), lambda: self._expand_all(False)))
            menu.addSeparator()
        if entry.get("filepath"):
            menu.addAction(make_action(menu, t, "folder", tr("Показать в папке"), lambda: self._show_in_folder(entry)))
            menu.addSeparator()
            menu.addAction(make_action(menu, t, "trash", tr("Удалить"), lambda: self._delete_entries([entry]), "danger"))
        if not menu.isEmpty():
            menu.exec(pos)
