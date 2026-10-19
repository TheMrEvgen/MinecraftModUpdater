"""Точка входа: графический интерфейс или консольный режим (см. cli.py)."""
import logging
import sys

import cli


def run_gui() -> int:
    import config as app_config
    from PyQt6.QtWidgets import QApplication
    from main_window import ModUpdaterApp

    app_config.setup_file_logging("mod_updater")
    logging.info("Запуск приложения Minecraft Mod Updater.")
    app = QApplication(sys.argv)
    window = ModUpdaterApp()
    window.show()
    return app.exec()


if __name__ == "__main__":
    if cli.wants_cli(sys.argv):
        sys.exit(cli.main())
    sys.exit(run_gui())
