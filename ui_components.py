from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QPushButton, QDialog, QVBoxLayout,
    QMessageBox, QStyle, QLineEdit, QApplication,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView
)
from PyQt6.QtCore import Qt

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
    box.setDefaultButton(box.addButton("ОК", QMessageBox.ButtonRole.AcceptRole))
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


def confirm(parent, title: str, text: str, yes_text: str = "Да", no_text: str = "Нет") -> bool:
    return ask_choice(parent, QMessageBox.Icon.Question, title, text, [yes_text, no_text], default=1) == 0

class ImportDialog(QDialog):
    """Диалоговое окно для ввода и импорта строки со списком модов."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Импортировать список модов")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setMinimumWidth(500)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0,0,0,0)
        main_layout.setSpacing(0)

        self.title_bar = DialogTitleBar(self)
        main_layout.addWidget(self.title_bar)

        content_widget = QWidget()
        layout = QVBoxLayout(content_widget)
        main_layout.addWidget(content_widget)
        
        info_label = QLabel("Вставьте сюда строку, полученную от друга:")
        layout.addWidget(info_label)

        self.import_text_edit = QLineEdit()
        self.import_text_edit.setPlaceholderText("Вставьте строку здесь...")
        layout.addWidget(self.import_text_edit)

        import_button = QPushButton("Импортировать")
        import_button.clicked.connect(self.accept) # accept() closes the dialog with a "Accepted" result
        layout.addWidget(import_button)

    def get_share_string(self):
        return self.import_text_edit.text()

class ShareDialog(QDialog):
    """Диалоговое окно для отображения и копирования строки для обмена модами."""
    def __init__(self, share_string, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Поделиться списком модов")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setMinimumWidth(500)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0,0,0,0)
        main_layout.setSpacing(0)

        self.title_bar = DialogTitleBar(self)
        main_layout.addWidget(self.title_bar)

        content_widget = QWidget()
        layout = QVBoxLayout(content_widget)
        main_layout.addWidget(content_widget)
        
        info_label = QLabel("Скопируйте эту строку и отправьте другу.\nОн сможет загрузить её через «Управление сборкой → Импортировать список».")
        layout.addWidget(info_label)

        self.share_text_edit = QLineEdit(share_string)
        self.share_text_edit.setReadOnly(True)
        layout.addWidget(self.share_text_edit)

        copy_button = QPushButton("Скопировать в буфер обмена")
        copy_button.clicked.connect(self.copy_to_clipboard)
        layout.addWidget(copy_button)

    def copy_to_clipboard(self):
        QApplication.clipboard().setText(self.share_text_edit.text())
        show_message(self, QMessageBox.Icon.Information, "Скопировано", "Строка скопирована в буфер обмена!")

class TitleBar(QWidget):
    """Пользовательский заголовок окна для поддержки тем."""
    def __init__(self, parent):
        super().__init__(parent)
        self.parent_window = parent
        self.setObjectName("TitleBar")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.title = QLabel("Minecraft Mod Updater")
        self.title.setStyleSheet("font-weight: bold;")
        
        self.toggle_theme_btn = QPushButton()
        self.toggle_theme_btn.setObjectName("ThemeButton")

        layout.addWidget(self.title)
        layout.addStretch()

        layout.addWidget(self.toggle_theme_btn)

        self.minimize_button = QPushButton("🗕")
        self.maximize_button = QPushButton("🗖")
        self.close_button = QPushButton("✕")
        
        self.minimize_button.setObjectName("MinimizeButton")
        self.maximize_button.setObjectName("MaximizeButton")
        self.close_button.setObjectName("CloseButton")

        for btn in [self.minimize_button, self.maximize_button, self.close_button]:
            btn.setFixedSize(45, 30)

        self.toggle_theme_btn.clicked.connect(self.parent_window.toggle_theme)
        self.minimize_button.clicked.connect(self.parent_window.showMinimized)
        self.maximize_button.clicked.connect(self.toggle_maximize_restore)
        self.close_button.clicked.connect(self.parent_window.close)

        layout.addWidget(self.minimize_button)
        layout.addWidget(self.maximize_button)
        layout.addWidget(self.close_button)

    def toggle_maximize_restore(self):
        if self.parent_window.isMaximized():
            self.parent_window.showNormal()
        else:
            self.parent_window.showMaximized()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.parent_window.drag_pos = event.globalPosition().toPoint() - self.parent_window.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and hasattr(self.parent_window, 'drag_pos'):
            self.parent_window.move(event.globalPosition().toPoint() - self.parent_window.drag_pos)
            event.accept()
    
    def mouseDoubleClickEvent(self, event):
        self.toggle_maximize_restore()
        event.accept()

class VersionPickerDialog(QDialog):
    """Диалоговое окно для выбора конкретной версии мода (Даунгрейд/Откат)."""
    def __init__(self, versions, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Выбор версии")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
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
        self.table.setHorizontalHeaderLabels(["Версия", "Тип", "Дата выхода"])
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
        ok_btn = QPushButton("Выбрать")
        ok_btn.clicked.connect(self.on_accept)
        cancel_btn = QPushButton("Отмена")
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
        self.setWindowTitle("Синхронизация по URL")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setMinimumWidth(500)
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0,0,0,0)
        main_layout.addWidget(DialogTitleBar(self))
        content = QWidget()
        layout = QVBoxLayout(content)
        main_layout.addWidget(content)
        layout.addWidget(QLabel("Введите URL к файлу конфигурации сборки сервера (JSON):"))
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://example.com/modpack.json")
        layout.addWidget(self.url_input)
        btn_layout = QHBoxLayout()
        ok_btn = QPushButton("Синхронизировать")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Отмена")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    def get_url(self):
        return self.url_input.text().strip()