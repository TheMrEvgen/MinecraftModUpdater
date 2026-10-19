from enum import IntEnum

class TableColumn(IntEnum):
    CHECKBOX = 0
    NAME = 1
    CURRENT_VERSION = 2
    NEW_VERSION = 3
    RELEASE_DATE = 4
    STATUS = 5
    ACTION = 6

# --- Цвета для тем ---
THEME_COLORS = {
    'dark': {
        'status_ok': "#66A569",
        'status_error': "#E57373",
        'update_available': "#4A442D",
        'updated': "#2E4B35",
        'update_error': "#5D383C",
        'downloading': "#2C435D",
        'ready_to_install': "#2D4234",
        'table_border': "#555",
    },
    'light': {
        'status_ok': "#43A047",
        'status_error': "red",
        'update_available': "#FFF3CD",
        'updated': "#D4EDDA",
        'update_error': "#F8D7DA",
        'downloading': "#CCE5FF",
        'table_border': "#C8C8C8",
        'ready_to_install': '#D1E7DD',
    }
}

# Стиль для темной темы
DARK_STYLE = """
    QWidget { background-color: #2b2b2b; color: #f0f0f0; font-size: 9pt; }
    QMainWindow, QDialog { background-color: #2b2b2b; }
    QGroupBox { border: 1px solid #555; border-radius: 6px; margin-top: 1ex; font-weight: bold; }
    QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 0 5px; left: 10px; }
    QLineEdit, QComboBox, QPlainTextEdit { background-color: #3c3c3c; border: 1px solid #555; border-radius: 4px; padding: 5px; }
    QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus { border: 1px solid #0078d7; }
    QPushButton { background-color: #555; border: 1px solid #666; border-radius: 4px; padding: 6px 12px; }
    QPushButton:hover { background-color: #666; border-color: #777; }
    QPushButton:pressed { background-color: #444; }
    QPushButton:disabled { background-color: #404040; color: #888; }
    QTableWidget { background-color: #3c3c3c; border: 1px solid #555; gridline-color: #555; }
    QTableWidget::item { padding: 5px; border-bottom: 1px solid #555; }
    QTableWidget::item:selected { background-color: #0078d7; color: #ffffff; }
    QHeaderView::section { background-color: #454545; color: #f0f0f0; padding: 5px; border: 1px solid #555; font-weight: bold; }
    QHeaderView::section:horizontal { border: none; }
    QTableCornerButton::section { border: none; background-color: #454545; }
    QMenu { background-color: #3c3c3c; border: 1px solid #555; }
    QMenu::item { padding: 6px 20px 6px 20px; }
    QMenu::item:selected { background-color: #0078d7; color: #ffffff; }
    QMenu::item:disabled { color: #888; }
    #TitleBar { background-color: #202020; }
    #TitleBar QLabel { padding-left: 10px; }
    #TitleBar QPushButton { border: none; background-color: transparent; font-size: 12pt; }
    #TitleBar QPushButton:hover { background-color: #555; }
    #TitleBar QPushButton:pressed { background-color: #444; }
    #CloseButton:hover { background-color: #E81123; }
    #CloseButton:pressed { background-color: #F1707A; }
"""

# Стиль для светлой темы
LIGHT_STYLE = """
    QWidget {
        background-color: #f0f0f0;
        color: #212121;
        font-size: 9pt;
    }
    QMainWindow, QDialog {
        background-color: #f0f0f0;
    }
    QGroupBox {
        border: 1px solid #dcdcdc;
        border-radius: 6px;
        margin-top: 1ex;
        font-weight: bold;
    }
    QGroupBox::title {
        subcontrol-origin: margin;
        subcontrol-position: top left;
        padding: 0 5px;
        left: 10px;
    }
    QLineEdit, QComboBox, QPlainTextEdit {
        background-color: #ffffff;
        border: 1px solid #cccccc;
        border-radius: 4px;
        padding: 5px;
    }
    QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus { border: 1px solid #0078d7; }
    QPushButton { background-color: #e0e0e0; border: 1px solid #b0b0b0; border-radius: 4px; padding: 6px 12px; }
    QPushButton:hover { background-color: #e8e8e8; border-color: #a0a0a0; }
    QPushButton:pressed { background-color: #d8d8d8; }
    QPushButton:disabled { background-color: #f5f5f5; color: #a0a0a0; }
    QTableWidget { background-color: #ffffff; border: 1px solid #dcdcdc; gridline-color: #e0e0e0; }
    QTableWidget::item { padding: 5px; border-bottom: 1px solid #e0e0e0; }
    QTableWidget::item:selected { background-color: #0078d7; color: #ffffff; }
    QHeaderView::section { background-color: #e8e8e8; color: #212121; padding: 5px; border: 1px solid #dcdcdc; font-weight: bold; }
    QHeaderView::section:horizontal { border: none; }
    QTableCornerButton::section { border: none; background-color: #e8e8e8; }
    QMenu { background-color: #ffffff; border: 1px solid #cccccc; }
    QMenu::item { padding: 6px 20px 6px 20px; }
    QMenu::item:selected { background-color: #0078d7; color: #ffffff; }
    QMenu::item:disabled { color: #a0a0a0; }
    #TitleBar { background-color: #dcdcdc; }
    #TitleBar QLabel { padding-left: 10px; }
    #TitleBar QPushButton { border: none; background-color: transparent; font-size: 12pt; }
    #TitleBar QPushButton:hover { background-color: #c8c8c8; }
    #TitleBar QPushButton:pressed { background-color: #b8b8b8; }
    #CloseButton:hover { background-color: #E81123; color: white; }
    #CloseButton:pressed { background-color: #F1707A; color: white; }
"""
