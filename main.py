"""Точка входа: графический интерфейс или консольный режим (см. cli.py)."""
import logging
import sys

import cli


def create_app():
    """QApplication с единым стилем и шрифтом интерфейса."""
    from PyQt6.QtGui import QFont, QFontDatabase
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Minecraft Mod Updater")
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
