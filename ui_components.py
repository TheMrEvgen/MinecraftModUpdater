import os

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QPushButton, QDialog, QVBoxLayout,
    QMessageBox, QStyle, QLineEdit, QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView
)
from PyQt6.QtCore import Qt
from i18n import tr

class DialogTitleBar(QWidget):
    """Пользовательский заголовок для диалоговых окон."""
    def __init__(self, parent):
        super().__init__(parent)
        self.parent_window = parent
        self.setObjectName("TitleBar")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.title = QLabel(f"{parent.windowTitle()}")
        self.title.setStyleSheet("font-weight: bold;")
        
        self.close_button = QPushButton("✕")
        self.close_button.setObjectName("CloseButton")
        self.close_button.setFixedSize(45, 30)
        self.close_button.clicked.connect(self.parent_window.reject) # Используем reject для закрытия диалога

        layout.addWidget(self.title)
        layout.addStretch()
        layout.addWidget(self.close_button)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.parent_window.drag_pos = event.globalPosition().toPoint() - self.parent_window.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and hasattr(self.parent_window, 'drag_pos'):
            self.parent_window.move(event.globalPosition().toPoint() - self.parent_window.drag_pos)
            event.accept()

class CustomMessageBox(QDialog):
    """Кастомный QMessageBox для поддержки тем в заголовке."""
    def __init__(self, icon, title, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setObjectName("DialogFrame")
        self.setMinimumWidth(400)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.title_bar = DialogTitleBar(self)
        main_layout.addWidget(self.title_bar)

        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.addWidget(content_widget)

        body_layout = QHBoxLayout()
        content_layout.addLayout(body_layout)

        if icon:
            # Сопоставляем QMessageBox.Icon с QStyle.StandardPixmap
            icon_map = {
                QMessageBox.Icon.Information: QStyle.StandardPixmap.SP_MessageBoxInformation,
                QMessageBox.Icon.Warning: QStyle.StandardPixmap.SP_MessageBoxWarning,
                QMessageBox.Icon.Critical: QStyle.StandardPixmap.SP_MessageBoxCritical,
                QMessageBox.Icon.Question: QStyle.StandardPixmap.SP_MessageBoxQuestion,
            }
            standard_pixmap_enum = icon_map.get(icon)
            if standard_pixmap_enum:
                icon_label = QLabel()
                pixmap = self.style().standardIcon(standard_pixmap_enum).pixmap(32, 32)
                icon_label.setPixmap(pixmap)
                body_layout.addWidget(icon_label)

        self.text_label = QLabel(text)
        self.text_label.setWordWrap(True)
        body_layout.addWidget(self.text_label, 1)

        self.button_layout = QHBoxLayout()
        self.button_layout.addStretch()
        content_layout.addLayout(self.button_layout)

        self._clicked_button = None

    def addButton(self, text, role):
        button = QPushButton(text)
        button.clicked.connect(lambda: self._button_clicked(button, role))
        self.button_layout.addWidget(button)
        return button

    def setDefaultButton(self, button):
        button.setDefault(True)
        button.setFocus()

    def _button_clicked(self, button, role):
        self._clicked_button = button
        if role in [QMessageBox.ButtonRole.AcceptRole, QMessageBox.ButtonRole.YesRole, QMessageBox.ButtonRole.ApplyRole]:
            self.accept()
        else:
            self.reject()
    
    def clickedButton(self):
        return self._clicked_button


def show_message(parent, icon, title: str, text: str) -> None:
    """Информационное окно с одной кнопкой «ОК»."""
    box = CustomMessageBox(icon, title, text, parent)
    box.setDefaultButton(box.addButton(tr("ОК"), QMessageBox.ButtonRole.AcceptRole))
    box.exec()


def ask_choice(parent, icon, title: str, text: str, buttons: list, default: int = 0,
               rich_text: bool = False) -> int | None:
    """
    Окно с выбором. buttons — список подписей; возвращает индекс нажатой кнопки
    или None, если окно закрыто крестиком.
    """
    box = CustomMessageBox(icon, title, "" if rich_text else text, parent)
    if rich_text:
        box.text_label.setTextFormat(Qt.TextFormat.RichText)
        box.text_label.setText(text)
    created = []
    for label in buttons:
        btn = QPushButton(label)
        btn.clicked.connect(lambda _=False, b=btn: (setattr(box, '_clicked_button', b), box.accept()))
        box.button_layout.addWidget(btn)
        created.append(btn)
    if created:
        box.setDefaultButton(created[default])
    box.exec()
    clicked = box.clickedButton()
    return created.index(clicked) if clicked in created else None


def confirm(parent, title: str, text: str, yes_text: str = tr("Да"), no_text: str = tr("Нет")) -> bool:
    return ask_choice(parent, QMessageBox.Icon.Question, title, text, [yes_text, no_text], default=1) == 0

class ImportDialog(QDialog):
    """Диалоговое окно для ввода и импорта строки со списком модов."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Импортировать список модов"))
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setObjectName("DialogFrame")
        self.setMinimumWidth(500)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0,0,0,0)
        main_layout.setSpacing(0)

        self.title_bar = DialogTitleBar(self)
        main_layout.addWidget(self.title_bar)

        content_widget = QWidget()
        layout = QVBoxLayout(content_widget)
        main_layout.addWidget(content_widget)
        
        info_label = QLabel(tr("Вставьте сюда строку, полученную от друга:"))
        layout.addWidget(info_label)

        self.import_text_edit = QLineEdit()
        self.import_text_edit.setPlaceholderText(tr("Вставьте строку здесь..."))
        layout.addWidget(self.import_text_edit)

        import_button = QPushButton(tr("Импортировать"))
        import_button.clicked.connect(self.accept) # accept() closes the dialog with a "Accepted" result
        layout.addWidget(import_button)

    def get_share_string(self):
        return self.import_text_edit.text()

class ShareDialog(QDialog):
    """Диалоговое окно для отображения и копирования строки для обмена модами."""
    def __init__(self, share_string, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Поделиться списком модов"))
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setObjectName("DialogFrame")
        self.setMinimumWidth(500)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0,0,0,0)
        main_layout.setSpacing(0)

        self.title_bar = DialogTitleBar(self)
        main_layout.addWidget(self.title_bar)

        content_widget = QWidget()
        layout = QVBoxLayout(content_widget)
        main_layout.addWidget(content_widget)
        
        info_label = QLabel(tr("Скопируйте эту строку и отправьте другу.\nОн сможет загрузить её через «Управление сборкой → Импортировать список»."))
        layout.addWidget(info_label)

        self.share_text_edit = QLineEdit(share_string)
        self.share_text_edit.setReadOnly(True)
        layout.addWidget(self.share_text_edit)

        copy_button = QPushButton(tr("Скопировать в буфер обмена"))
        copy_button.clicked.connect(self.copy_to_clipboard)
        layout.addWidget(copy_button)

    def copy_to_clipboard(self):
        QApplication.clipboard().setText(self.share_text_edit.text())
        show_message(self, QMessageBox.Icon.Information, tr("Скопировано"), tr("Строка скопирована в буфер обмена!"))

class VersionPickerDialog(QDialog):
    """Диалоговое окно для выбора конкретной версии мода (Даунгрейд/Откат)."""
    def __init__(self, versions, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Выбор версии"))
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setObjectName("DialogFrame")
        self.setMinimumSize(600, 400)
        self.selected_version = None

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0,0,0,0)
        main_layout.setSpacing(0)
        main_layout.addWidget(DialogTitleBar(self))

        content = QWidget()
        layout = QVBoxLayout(content)
        main_layout.addWidget(content)

        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels([tr("Версия"), tr("Тип"), tr("Дата выхода")])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)

        self.table.setRowCount(len(versions))
        self.versions = versions
        for i, v in enumerate(versions):
            self.table.setItem(i, 0, QTableWidgetItem(v.get('version_number', 'N/A')))
            self.table.setItem(i, 1, QTableWidgetItem(v.get('version_type', 'N/A')))
            date_str = v.get('date_published', '')
            if date_str: date_str = date_str.split('T')[0]
            self.table.setItem(i, 2, QTableWidgetItem(date_str))

        btn_layout = QHBoxLayout()
        ok_btn = QPushButton(tr("Выбрать"))
        ok_btn.clicked.connect(self.on_accept)
        cancel_btn = QPushButton(tr("Отмена"))
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    def on_accept(self):
        selected = self.table.selectedItems()
        if selected:
            self.selected_version = self.versions[selected[0].row()]
            self.accept()

class SyncDialog(QDialog):
    """Диалог для ввода URL синхронизации с сервером."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Синхронизация по URL"))
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setObjectName("DialogFrame")
        self.setMinimumWidth(500)
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0,0,0,0)
        main_layout.addWidget(DialogTitleBar(self))
        content = QWidget()
        layout = QVBoxLayout(content)
        main_layout.addWidget(content)
        layout.addWidget(QLabel(tr("Введите URL к файлу конфигурации сборки сервера (JSON):")))
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://example.com/modpack.json")
        layout.addWidget(self.url_input)
        btn_layout = QHBoxLayout()
        ok_btn = QPushButton(tr("Синхронизировать"))
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton(tr("Отмена"))
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    def get_url(self):
        return self.url_input.text().strip()


PROFILE_ICONS = (("", tr("Точка")), ("package", tr("Коробка")), ("layers", tr("Слои")), ("flame", tr("Огонь")),
                 ("shield", tr("Щит")), ("star", tr("Звезда")), ("heart", tr("Сердце")), ("compass", tr("Компас")),
                 ("code", tr("Код")), ("sparkles", tr("Искры")), ("globe", tr("Глобус")))


class ProfileDialog(QDialog):
    """Создание и редактирование сборки: папка игры, версия, загрузчик, канал обновлений."""

    LOADERS = ["fabric", "neoforge", "forge", "quilt"]

    def __init__(self, parent=None, name: str = "", profile: dict | None = None, existing_names=(), channels=(),
                 groups=()):
        super().__init__(parent)
        self.setWindowTitle(tr("Изменить сборку") if name else tr("Новая сборка"))
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setObjectName("DialogFrame")
        self.setMinimumWidth(580)
        self.existing_names = set(existing_names) - {name}
        self.profile = dict(profile or {})

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addWidget(DialogTitleBar(self))
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 16, 20, 18)
        layout.setSpacing(12)
        main_layout.addWidget(content)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self.name_edit = QLineEdit(name)
        self.name_edit.setPlaceholderText(tr("Например: Выживание с друзьями"))
        form.addRow(tr("Название"), self.name_edit)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(self.profile.get("mods_path", ""))
        self.path_edit.setPlaceholderText(tr("Папка игры, внутри которой лежит папка mods"))
        browse = QPushButton(tr("Обзор…"))
        browse.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        form.addRow(tr("Папка игры"), path_row)
        self.path_status = QLabel()
        self.path_status.setObjectName("Faint")
        form.addRow("", self.path_status)

        self.manual = QCheckBox(tr("Указать версию игры и загрузчик вручную"))
        self.manual.setChecked(bool(self.profile.get("manual_mode")))
        form.addRow("", self.manual)
        self.version_combo = QComboBox()
        self.version_combo.setEditable(True)
        self.loader_combo = QComboBox()
        self.loader_combo.addItems(self.LOADERS)
        form.addRow(tr("Версия игры"), self.version_combo)
        form.addRow(tr("Загрузчик"), self.loader_combo)
        self.channel_combo = QComboBox()
        for value, title in channels:
            self.channel_combo.addItem(title, value)
        self.channel_combo.setCurrentIndex(max(0, self.channel_combo.findData(self.profile.get("channel"))))
        form.addRow(tr("Обновления"), self.channel_combo)

        # Оформление сборки в боковой панели: группа, цвет, значок
        from theme import ACCENTS
        self.group_combo = QComboBox()
        self.group_combo.setEditable(True)
        self.group_combo.addItem("")
        self.group_combo.addItems(sorted(g for g in set(groups) if g))
        self.group_combo.setCurrentText(self.profile.get("group") or "")
        self.group_combo.lineEdit().setPlaceholderText(tr("Без группы — например, «Серверы»"))
        form.addRow(tr("Группа"), self.group_combo)
        self.color_combo = QComboBox()
        self.color_combo.addItem(tr("Как у темы"), "")
        for key, (title, dark, _light) in ACCENTS.items():
            self.color_combo.addItem(title, dark)
        index = self.color_combo.findData(self.profile.get("color") or "")
        self.color_combo.setCurrentIndex(max(0, index))
        form.addRow(tr("Цвет"), self.color_combo)
        self.icon_combo = QComboBox()
        for key, title in PROFILE_ICONS:
            self.icon_combo.addItem(title, key)
        self.icon_combo.setCurrentIndex(max(0, self.icon_combo.findData(self.profile.get("icon") or "")))
        form.addRow(tr("Значок"), self.icon_combo)
        self.launch_edit = QLineEdit(self.profile.get("launch_command") or "")
        self.launch_edit.setPlaceholderText(tr("Необязательно: своя команда для кнопки «Играть»"))
        self.launch_edit.setToolTip(tr("Prism, MultiMC и официальный лаунчер находятся сами. Здесь можно указать свою "
                                    'команду, например: "C:\\Games\\launcher.exe" --profile Survival'))
        form.addRow(tr("Запуск игры"), self.launch_edit)
        layout.addLayout(form)

        self.error_label = QLabel()
        self.error_label.setStyleSheet("color: #f25f5c;")
        self.error_label.hide()
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton(tr("Отмена"))
        cancel.clicked.connect(self.reject)
        ok = QPushButton(tr("Сохранить"))
        ok.setObjectName("PrimaryButton")
        ok.setDefault(True)
        ok.clicked.connect(self._accept)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        layout.addLayout(buttons)

        self.path_edit.textChanged.connect(self._on_path_changed)
        self.manual.toggled.connect(self._sync_enabled)
        self._on_path_changed(self.path_edit.text())
        if self.profile.get("game_version"):
            self.version_combo.setCurrentText(self.profile["game_version"])
        if self.profile.get("loader") in self.LOADERS:
            self.loader_combo.setCurrentText(self.profile["loader"])
        self._sync_enabled()

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, tr("Папка игры (где находится папка mods)"), self.path_edit.text())
        if path:
            self.path_edit.setText(path)

    def _on_path_changed(self, path: str):
        from game_detection import detect_game_versions
        mods_dir = os.path.join(path, "mods") if path else ""
        if not path:
            self.path_status.setText("")
        elif os.path.isdir(mods_dir):
            count = len([f for f in os.listdir(mods_dir) if f.lower().endswith(".jar")])
            self.path_status.setText(tr('✓ Папка mods найдена, модов: {0}', count))
        elif os.path.isdir(path):
            self.path_status.setText(tr("Папки mods нет — она будет создана при первой установке"))
        else:
            self.path_status.setText(tr("⚠ Папка не существует"))
        detected = detect_game_versions(path) if path and os.path.isdir(path) else None
        current = self.version_combo.currentText()
        self.version_combo.clear()
        if detected and detected.versions:
            self.version_combo.addItems(detected.game_versions)
            if not self.manual.isChecked() and detected.best_guess:
                self.version_combo.setCurrentText(detected.best_guess[0])
                self.loader_combo.setCurrentText(detected.best_guess[1])
        if current and (self.manual.isChecked() or not (detected and detected.versions)):
            self.version_combo.setCurrentText(current)

    def _sync_enabled(self):
        manual = self.manual.isChecked()
        self.version_combo.setEnabled(manual)
        self.loader_combo.setEnabled(manual)

    def _accept(self):
        name = self.name_edit.text().strip()
        path = self.path_edit.text().strip()
        error = None
        if not name:
            error = tr("Введите название сборки.")
        elif name in self.existing_names:
            error = tr("Сборка с таким названием уже есть.")
        elif not path or not os.path.isdir(path):
            error = tr("Укажите существующую папку игры.")
        elif self.manual.isChecked() and not self.version_combo.currentText().strip():
            error = tr("Укажите версию игры.")
        if error:
            self.error_label.setText(error)
            self.error_label.show()
            return
        self.accept()

    def result_profile(self) -> tuple[str, dict]:
        profile = dict(self.profile)
        profile.update({
            "mods_path": self.path_edit.text().strip(),
            "manual_mode": self.manual.isChecked(),
            "game_version": self.version_combo.currentText().strip(),
            "loader": self.loader_combo.currentText(),
            "channel": self.channel_combo.currentData(),
            "group": self.group_combo.currentText().strip(),
            "color": self.color_combo.currentData() or "",
            "icon": self.icon_combo.currentData() or "",
            "launch_command": self.launch_edit.text().strip(),
        })
        profile.setdefault("source", "manual")
        return self.name_edit.text().strip(), profile