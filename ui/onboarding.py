"""Мастер первого запуска: язык и тема → сборки → установка программы."""
from typing import Dict, List, Optional

from PyQt6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QHBoxLayout, QLabel, QListWidget, QRadioButton, QStackedWidget, QVBoxLayout,
    QWidget,
)

from i18n import LANGUAGES, language, tr
from theme import THEME_TITLES
from ui.dialogs import FramelessDialog
from ui.widgets import label


class OnboardingDialog(FramelessDialog):
    """Три шага. После exec(): language, theme, add_manually, install_options (None — не устанавливать)."""

    def __init__(self, parent, theme: str, profiles: List[Dict], can_install: bool, installed: bool):
        super().__init__(parent, tr("Добро пожаловать"), 560)
        self.setMinimumHeight(440)
        self.language = language()
        self.theme = theme
        self.add_manually = False
        self.install_options: Optional[Dict] = None
        self.can_install = can_install and not installed

        self.step_label = label("", "Faint")
        self.body.addWidget(self.step_label)
        self.pages = QStackedWidget()
        self.body.addWidget(self.pages, 1)
        self.pages.addWidget(self._appearance_page())
        self.pages.addWidget(self._profiles_page(profiles))
        self.pages.addWidget(self._install_page(can_install, installed))

        self.skip_btn = self.add_button(tr("Пропустить"), False, self.reject)
        self.back_btn = self.add_button(tr("Назад"), False, lambda: self._go(-1))
        self.next_btn = self.add_button(tr("Далее"), True, lambda: self._go(1))
        self.finish_layout()
        self._go(0)

    # --- Шаги ---

    def _appearance_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addWidget(label(tr("Minecraft Mod Updater следит за модами ваших сборок: находит обновления на Modrinth и "
                               "CurseForge, ставит их вместе с зависимостями и позволяет откатить любое изменение."),
                            "Muted", wrap=True))
        lay.addSpacing(6)
        row = QHBoxLayout()
        row.addWidget(QLabel("Язык / Language:"))
        self.language_combo = QComboBox()
        for code, title in LANGUAGES.items():
            self.language_combo.addItem(title, code)
        self.language_combo.setCurrentIndex(max(0, self.language_combo.findData(self.language)))
        row.addWidget(self.language_combo)
        row.addStretch()
        lay.addLayout(row)
        lay.addSpacing(6)
        lay.addWidget(label(tr("Тема"), "SectionTitle"))
        self.theme_group = QButtonGroup(self)
        themes = QHBoxLayout()
        for key, title in THEME_TITLES.items():
            radio = QRadioButton(title)
            radio.setProperty("theme_key", key)
            radio.setChecked(key == self.theme)
            radio.toggled.connect(lambda on, k=key: on and self._set_theme(k))
            self.theme_group.addButton(radio)
            themes.addWidget(radio)
        themes.addStretch()
        lay.addLayout(themes)
        lay.addStretch()
        return page

    def _profiles_page(self, profiles: List[Dict]) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        if profiles:
            lay.addWidget(label(tr("Найдено сборок в лаунчерах: {0}. Они уже в списке слева — откройте любую, "
                                   "и программа проверит её моды.", len(profiles)), "Muted", wrap=True))
            found = QListWidget()
            for profile in profiles:
                found.addItem(f"{profile['name']}   ·   {profile.get('subtitle', '')}")
            found.setSelectionMode(QListWidget.SelectionMode.NoSelection)
            lay.addWidget(found, 1)
        else:
            lay.addWidget(label(tr("Сборок в Prism Launcher, MultiMC, Modrinth App, CurseForge и официальном лаунчере "
                                   "не найдено. Укажите папку игры — ту, где лежит папка mods."), "Muted", wrap=True))
        self.manual_box = QCheckBox(tr("Указать папку игры вручную после мастера"))
        self.manual_box.setChecked(not profiles)
        lay.addWidget(self.manual_box)
        if profiles:
            lay.addWidget(label(tr("Новые сборки из лаунчеров программа находит сама при каждом запуске."), "Faint", wrap=True))
        else:
            lay.addStretch()
        return page

    def _install_page(self, can_install: bool, installed: bool) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        if installed:
            lay.addWidget(label(tr("Программа уже установлена — всё готово."), "Muted", wrap=True))
        elif not can_install:
            lay.addWidget(label(tr("Установка доступна в собранной программе (.exe) для Windows. Сейчас программа "
                                   "запущена из исходников — этот шаг можно пропустить."), "Muted", wrap=True))
        else:
            lay.addWidget(label(tr("Программа запущена просто как .exe-файл. Установка займёт пару секунд и не требует "
                                   "прав администратора; удалить можно через «Установку и удаление программ»."),
                                "Muted", wrap=True))
            self.install_box = QCheckBox(tr("Установить программу"))
            self.install_box.setChecked(True)
            lay.addWidget(self.install_box)
            self.desktop_box = QCheckBox(tr("Ярлык на рабочем столе"))
            self.mrpack_box = QCheckBox(tr("Открывать файлы .mrpack двойным щелчком"))
            self.protocol_box = QCheckBox(tr("Открывать ссылки modupdater:// (кнопки «Открыть в Mod Updater» на сайтах)"))
            for box in (self.desktop_box, self.mrpack_box, self.protocol_box):
                box.setChecked(True)
                box.setContentsMargins(24, 0, 0, 0)
                self.install_box.toggled.connect(box.setEnabled)
                lay.addWidget(box)
        lay.addStretch()
        lay.addWidget(label(tr("Всё это можно поменять потом в «Настройках»."), "Faint", wrap=True))
        return page

    # --- Переходы ---

    def _set_theme(self, theme: str):
        self.theme = theme
        parent = self.parent()
        if parent is not None and hasattr(parent, "_on_theme_selected"):
            parent._on_theme_selected(theme)  # Тема меняется сразу — видно, как будет выглядеть

    def _go(self, step: int):
        index = self.pages.currentIndex() + step
        if index >= self.pages.count():
            self._finish()
            return
        self.pages.setCurrentIndex(max(0, index))
        index = self.pages.currentIndex()
        titles = (tr("Язык и оформление"), tr("Сборки"), tr("Установка программы"))
        self.step_label.setText(tr("Шаг {0} из {1} · {2}", index + 1, self.pages.count(), titles[index]))
        self.back_btn.setVisible(index > 0)
        self.next_btn.setText(tr("Готово") if index == self.pages.count() - 1 else tr("Далее"))

    def _finish(self):
        self.language = self.language_combo.currentData()
        self.add_manually = self.manual_box.isChecked()
        if self.can_install and self.install_box.isChecked():
            self.install_options = {"desktop": self.desktop_box.isChecked(), "associate_mrpack": self.mrpack_box.isChecked(),
                                    "protocol": self.protocol_box.isChecked()}
        self.accept()
