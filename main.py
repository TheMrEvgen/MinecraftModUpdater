"""Точка входа: графический интерфейс или консольный режим (см. cli.py)."""
import logging
import sys

import cli


def create_app():
    """QApplication с единым стилем и шрифтом интерфейса."""
    from PyQt6.QtGui import QFont, QFontDatabase
    from PyQt6.QtWidgets import QApplication

    import os
    import config as app_config
    from PyQt6.QtGui import QIcon

    if sys.platform == "win32":
        # Отдельный AppUserModelID: Windows показывает в панели задач нашу иконку, а не иконку python.exe
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TheMrEvgen.MinecraftModUpdater")
        except (OSError, AttributeError):
            pass
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Minecraft Mod Updater")
    icon_path = app_config.resource_path(app_config.APP_ICON)
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    app.setStyle("Fusion")  # Предсказуемая отрисовка таблицы стилей на любой системе
    families = set(QFontDatabase.families())
    family = next((f for f in ("Segoe UI Variable Text", "Segoe UI", "Inter", "Ubuntu", "Cantarell")
                   if f in families), app.font().family())
    font = QFont(family, 10)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)
    return app


def run_gui() -> int:
    import config as app_config
    from main_window import ModUpdaterApp

    app_config.setup_file_logging("mod_updater")
    logging.info("Запуск приложения Minecraft Mod Updater.")
    app = create_app()
    window = ModUpdaterApp()
    window.show()
    return app.exec()


if __name__ == "__main__":
    if cli.wants_cli(sys.argv):
        sys.exit(cli.main())
    sys.exit(run_gui())
