"""Дизайн-токены, генерация таблицы стилей и тёмный заголовок окна Windows."""
import logging
import sys

from PyQt6.QtGui import QColor

DARK = {
    "name": "dark",
    "bg": "#0f1115",
    "sidebar": "#0b0d11",
    "surface": "#161920",
    "surface2": "#1d212a",
    "surface3": "#262b36",
    "border": "#252a35",
    "border_strong": "#323846",
    "text": "#e9ecf2",
    "text_muted": "#8f97ab",
    "text_faint": "#5d6579",
    "accent": "#1bd96a",
    "accent_hover": "#3ae583",
    "accent_pressed": "#14b558",
    "accent_text": "#03160b",
    "info": "#5aa2ff",
    "warning": "#f5a524",
    "danger": "#f25f5c",
    "success": "#1bd96a",
    "selection": "#18241e",
    "shadow": "#000000",
}

LIGHT = {
    "name": "light",
    "bg": "#f5f6f8",
    "sidebar": "#eceef2",
    "surface": "#ffffff",
    "surface2": "#f1f3f6",
    "surface3": "#e5e8ee",
    "border": "#e3e6ec",
    "border_strong": "#d3d7df",
    "text": "#141821",
    "text_muted": "#5c6477",
    "text_faint": "#99a0b0",
    "accent": "#0ea65a",
    "accent_hover": "#12b866",
    "accent_pressed": "#0b8a4b",
    "accent_text": "#ffffff",
    "info": "#2f6fdf",
    "warning": "#c27400",
    "danger": "#d63b3b",
    "success": "#0ea65a",
    "selection": "#e3f5eb",
    "shadow": "#1a2030",
}

THEMES = {"dark": DARK, "light": LIGHT}


def rgba(hex_color: str, alpha: float) -> str:
    c = QColor(hex_color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


def qcolor(hex_color: str, alpha: float = 1.0) -> QColor:
    c = QColor(hex_color)
    c.setAlphaF(alpha)
    return c


def build_stylesheet(t: dict, check_icon: str = "") -> str:
    """Таблица стилей приложения из токенов темы. check_icon — путь к SVG галочки."""
    check_image = f"image: url({check_icon});" if check_icon else ""
    return f"""
* {{ outline: none; }}
QWidget {{ color: {t['text']}; font-size: 10pt; }}
QMainWindow, QDialog, #Page, #ContentRoot {{ background: {t['bg']}; }}
QToolTip {{ background: {t['surface3']}; color: {t['text']}; border: 1px solid {t['border_strong']};
            border-radius: 6px; padding: 6px 8px; }}

/* --- Боковая панель --- */
#Sidebar {{ background: {t['sidebar']}; border-right: 1px solid {t['border']}; }}
#AppTitle {{ font-size: 12pt; font-weight: 700; }}
#AppVersion, #SectionLabel {{ color: {t['text_faint']}; font-size: 8pt; font-weight: 600; letter-spacing: 1px; }}
#ProfileList {{ background: transparent; border: none; }}
#NavButton {{ text-align: left; padding: 9px 12px; border: none; border-radius: 8px; background: transparent;
              color: {t['text_muted']}; font-weight: 600; }}
#NavButton:hover {{ background: {t['surface2']}; color: {t['text']}; }}
#NavButton:checked {{ background: {rgba(t['accent'], 0.12)}; color: {t['accent']}; }}

/* --- Кнопки --- */
QPushButton {{ background: {t['surface2']}; border: 1px solid {t['border_strong']}; border-radius: 8px;
               padding: 7px 14px; font-weight: 600; }}
QPushButton:hover {{ background: {t['surface3']}; }}
QPushButton:pressed {{ background: {t['border_strong']}; }}
QPushButton:disabled {{ color: {t['text_faint']}; background: {t['surface']}; border-color: {t['border']}; }}
QPushButton::menu-indicator {{ width: 0; image: none; }}
#PrimaryButton {{ background: {t['accent']}; color: {t['accent_text']}; border: 1px solid {t['accent']}; }}
#PrimaryButton:hover {{ background: {t['accent_hover']}; border-color: {t['accent_hover']}; }}
#PrimaryButton:pressed {{ background: {t['accent_pressed']}; }}
#PrimaryButton:disabled {{ background: {rgba(t['accent'], 0.25)}; color: {rgba(t['accent_text'], 0.6)};
                           border-color: transparent; }}
#GhostButton, #IconButton {{ background: transparent; border: 1px solid transparent; }}
#GhostButton:hover, #IconButton:hover {{ background: {t['surface2']}; border-color: {t['border']}; }}
#IconButton {{ padding: 6px; border-radius: 8px; }}
#DangerButton {{ background: transparent; color: {t['danger']}; border: 1px solid {rgba(t['danger'], 0.35)}; }}
#DangerButton:hover {{ background: {rgba(t['danger'], 0.12)}; }}

/* --- Чипы в шапке сборки --- */
#Chip {{ background: {t['surface2']}; border: 1px solid {t['border']}; border-radius: 13px;
         padding: 4px 11px; font-weight: 600; color: {t['text_muted']}; }}
#Chip:hover {{ border-color: {t['border_strong']}; color: {t['text']}; }}
#Chip[accent="true"] {{ background: {rgba(t['accent'], 0.12)}; border-color: {rgba(t['accent'], 0.35)}; color: {t['accent']}; }}
#Chip[warning="true"] {{ background: {rgba(t['warning'], 0.12)}; border-color: {rgba(t['warning'], 0.4)}; color: {t['warning']}; }}
#PathLabel {{ color: {t['text_faint']}; }}
#PathLabel:hover {{ color: {t['text_muted']}; }}

#PageTitle {{ font-size: 18pt; font-weight: 700; }}
#PageSubtitle, #Muted {{ color: {t['text_muted']}; }}
#Faint {{ color: {t['text_faint']}; font-size: 9pt; }}
#SectionTitle {{ font-size: 11pt; font-weight: 700; }}

/* --- Карточки-сводки (работают как фильтры) --- */
#StatCard {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 12px;
             padding: 10px 14px; text-align: left; }}
#StatCard:hover {{ border-color: {t['border_strong']}; background: {t['surface2']}; }}
#StatCard:checked {{ border: 1px solid {rgba(t['accent'], 0.6)}; background: {rgba(t['accent'], 0.08)}; }}

/* --- Поля ввода --- */
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextBrowser {{
    background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 8px; padding: 6px 10px;
    selection-background-color: {rgba(t['accent'], 0.35)}; selection-color: {t['text']}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {{ border-color: {rgba(t['accent'], 0.7)}; }}
QLineEdit:disabled, QComboBox:disabled {{ color: {t['text_faint']}; }}
#SearchBox {{ padding-left: 34px; border-radius: 10px; min-height: 22px; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{ image: url({check_icon.replace('check', 'chevron')}); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{ background: {t['surface2']}; border: 1px solid {t['border_strong']};
    border-radius: 8px; padding: 4px; selection-background-color: {rgba(t['accent'], 0.18)}; selection-color: {t['text']}; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px; border-radius: 5px;
    border: 1.5px solid {t['border_strong']}; background: {t['surface']}; }}
QRadioButton {{ spacing: 8px; }}
QRadioButton::indicator {{ border-radius: 9px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {t['accent']}; }}
QCheckBox::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; {check_image} }}
QRadioButton::indicator:checked {{ border-color: {t['accent']};
    background: qradialgradient(cx: 0.5, cy: 0.5, radius: 0.5, fx: 0.5, fy: 0.5, stop: 0 {t['accent']},
                                stop: 0.5 {t['accent']}, stop: 0.62 {t['surface']}, stop: 1 {t['surface']}); }}
QCheckBox::indicator:indeterminate {{ background: {rgba(t['accent'], 0.5)}; border-color: {t['accent']}; }}

/* --- Меню --- */
QMenu {{ background: {t['surface2']}; border: 1px solid {t['border_strong']}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 26px 7px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {rgba(t['accent'], 0.14)}; color: {t['text']}; }}
QMenu::item:disabled {{ color: {t['text_faint']}; }}
QMenu::separator {{ height: 1px; background: {t['border']}; margin: 5px 8px; }}
QMenu::icon {{ padding-left: 8px; }}

/* --- Список модов и панели --- */
#ModList {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 12px; }}
#ListHeader {{ color: {t['text_muted']}; }}
#DetailsPanel {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 12px; }}
QListWidget {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 10px;
                          color: {t['text']}; }}
QListWidget::item:selected {{ background: {rgba(t['accent'], 0.14)}; color: {t['text']}; }}
QListWidget::item:hover {{ background: {t['surface2']}; }}
#VersionList {{ background: {t['bg']}; padding: 4px; }}
#ReviewList {{ background: {t['bg']}; padding: 4px; }}
#ReviewList::item {{ padding: 4px 6px; border-radius: 8px; }}
#ReviewList::item:selected {{ background: {rgba(t['accent'], 0.14)}; color: {t['text']}; }}
#Segment {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 9px; padding: 7px 14px;
            color: {t['text_muted']}; font-weight: 600; }}
#Segment:hover {{ background: {t['surface2']}; color: {t['text']}; }}
#Segment:checked {{ background: {rgba(t['accent'], 0.12)}; border-color: {rgba(t['accent'], 0.45)}; color: {t['accent']}; }}
#Palette {{ background: {t['surface']}; border: 1px solid {t['border_strong']}; border-radius: 14px; }}
#PaletteInput {{ font-size: 12pt; padding: 10px 12px; border-radius: 10px; background: {t['bg']}; }}
#PaletteList {{ background: transparent; border: none; }}
#DetailsTitle {{ font-size: 14pt; font-weight: 700; }}
#Card {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 12px; }}
#Divider {{ background: {t['border']}; max-height: 1px; min-height: 1px; }}
#StatusPill {{ border-radius: 10px; padding: 3px 10px; font-weight: 600; font-size: 9pt; }}
#Banner {{ background: {rgba(t['info'], 0.1)}; border: 1px solid {rgba(t['info'], 0.35)}; border-radius: 10px; }}
#SelectionBar {{ background: {t['surface3']}; border: 1px solid {t['border_strong']}; border-radius: 12px; }}
#DropOverlay {{ background: {rgba(t['bg'], 0.85)}; border: 2px dashed {t['accent']}; border-radius: 16px; }}
#DropOverlay QLabel {{ color: {t['accent']}; font-size: 15pt; font-weight: 700; background: transparent; }}

#Toast {{ background: {t['surface3']}; border: 1px solid {t['border_strong']}; border-radius: 12px; }}
#Toast QLabel {{ background: transparent; }}
#ToastAction {{ background: transparent; border: none; color: {t['accent']}; padding: 4px 8px; }}
#ToastAction:hover {{ background: {rgba(t['accent'], 0.12)}; }}

QProgressBar#SlimProgress {{ background: transparent; border: none; max-height: 3px; min-height: 3px; }}
QProgressBar#SlimProgress::chunk {{ background: {t['accent']}; border-radius: 1px; }}
QProgressBar {{ background: {t['surface3']}; border: none; border-radius: 4px; text-align: center; }}
QProgressBar::chunk {{ background: {t['accent']}; border-radius: 4px; }}

/* --- Полосы прокрутки --- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}
QScrollBar::handle:vertical {{ background: {t['surface3']}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {t['border_strong']}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px 4px; }}
QScrollBar::handle:horizontal {{ background: {t['surface3']}; border-radius: 3px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}

/* --- Диалоги без рамки (ui_components) --- */
#TitleBar {{ background: {t['surface']}; border-bottom: 1px solid {t['border']}; }}
#TitleBar QLabel {{ padding-left: 14px; }}
#TitleBar QPushButton {{ border: none; border-radius: 0; background: transparent; font-size: 11pt; }}
#TitleBar QPushButton:hover {{ background: {t['surface3']}; }}
#CloseButton:hover {{ background: #e81123; color: white; }}
#DialogFrame {{ background: {t['bg']}; border: 1px solid {t['border_strong']}; }}
QTableWidget {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 8px;
                gridline-color: {t['border']}; }}
QHeaderView::section {{ background: {t['surface2']}; border: none; padding: 6px; font-weight: 600; }}
QTableWidget::item:selected {{ background: {rgba(t['accent'], 0.2)}; color: {t['text']}; }}
"""


def detect_system_theme(fallback_dark: bool = True) -> str:
    """'dark' или 'light' по настройке Windows (AppsUseLightTheme)."""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as key:
                value, _ = winreg.QueryValueEx(key, 'AppsUseLightTheme')
            return 'light' if value else 'dark'
        except OSError:
            pass
    return 'dark' if fallback_dark else 'light'


def apply_window_frame_theme(widget, tokens: dict) -> None:
    """
    Окрашивает системный заголовок окна под тему: тёмный режим (Windows 10 20H1+)
    и цвет заголовка (Windows 11). На других системах ничего не делает.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = wintypes.HWND(int(widget.winId()))
        dwm = ctypes.windll.dwmapi
        dark = ctypes.c_int(1 if tokens["name"] == "dark" else 0)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (новый и старый номер)
            if dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(dark), ctypes.sizeof(dark)) == 0:
                break
        color = QColor(tokens["sidebar"])
        colorref = ctypes.c_uint(color.red() | (color.green() << 8) | (color.blue() << 16))
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(colorref), ctypes.sizeof(colorref))  # DWMWA_CAPTION_COLOR
        # Windows 10 перерисовывает заголовок только при смене активности окна: «переключаем» её вручную
        user32 = ctypes.windll.user32
        active = hwnd.value == user32.GetForegroundWindow()
        user32.SendMessageW(hwnd, 0x0086, 0 if active else 1, 0)  # WM_NCACTIVATE
        user32.SendMessageW(hwnd, 0x0086, 1 if active else 0, 0)
        user32.RedrawWindow(hwnd, None, None, 0x0400 | 0x0001 | 0x0100)  # RDW_FRAME | RDW_INVALIDATE | RDW_UPDATENOW
    except (OSError, AttributeError) as e:
        logging.debug(f"Не удалось настроить заголовок окна: {e}")
