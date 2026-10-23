"""Точка входа: графический интерфейс или консольный режим (см. cli.py)."""
import logging
import sys

import cli
from i18n import tr


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
    install_qt_translator(app)
    families = set(QFontDatabase.families())
    family = next((f for f in ("Segoe UI Variable Text", "Segoe UI", "Inter", "Ubuntu", "Cantarell")
                   if f in families), app.font().family())
    font = QFont(family, 10)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)
    return app


def install_qt_translator(app) -> None:
    """Стандартные тексты Qt (кнопки диалогов, меню полей ввода) на языке программы."""
    from PyQt6.QtCore import QLibraryInfo, QLocale, QTranslator
    import i18n
    QLocale.setDefault(QLocale(QLocale.Language.Russian if i18n.language() == "ru" else QLocale.Language.English))
    if i18n.language() != "ru":
        return  # Встроенные тексты Qt уже английские
    translator = QTranslator(app)
    if translator.load("qtbase_ru", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(translator)
        app._qt_translator = translator  # Держим ссылку, пока живёт приложение


def apply_ui_scale() -> None:
    """Масштаб интерфейса из настроек: задаётся до создания QApplication, поэтому меняется после перезапуска."""
    import os
    import config as app_config
    scale = int(app_config.load_config()["settings"].get("ui_scale", 100) or 100)
    if scale != 100 and "QT_SCALE_FACTOR" not in os.environ:
        os.environ["QT_SCALE_FACTOR"] = str(scale / 100)


def run_uninstall(quiet: bool) -> int:
    """«Удалить» из «Установки и удаления программ» Windows (или --uninstall вручную)."""
    import config as app_config
    import installer
    app_config.setup_file_logging("mod_updater_uninstall")
    if quiet:
        installer.uninstall()
        return 0
    create_app()
    from ui.dialogs import FramelessDialog
    from ui.widgets import label
    from PyQt6.QtWidgets import QCheckBox
    dialog = FramelessDialog(None, tr("Удаление Minecraft Mod Updater"), 480)
    dialog.body.addWidget(label(tr("Будут удалены программа, ярлыки и связь с файлами .mrpack и ссылками modupdater://. "
                                "Ваши сборки, моды и бэкапы в папках игры не затрагиваются."), None, wrap=True))
    remove_data = QCheckBox(tr("Удалить также настройки, заметки и кэш программы"))
    dialog.body.addWidget(remove_data)
    dialog.add_button(tr("Отмена"))
    dialog.add_button(tr("Удалить"), True, dialog.accept)
    dialog.finish_layout()
    if not dialog.exec():
        return 1
    installer.uninstall(remove_data=remove_data.isChecked())
    return 0


def run_gui() -> int:
    import config as app_config
    import single_instance

    app_config.setup_file_logging("mod_updater")
    apply_ui_scale()
    logging.info(tr("Запуск приложения Minecraft Mod Updater."))
    app = create_app()
    args = single_instance.external_args(sys.argv)
    if single_instance.RESTART_FLAG in sys.argv:
        single_instance.wait_for_previous()  # Перезапуск или обновление: прежняя копия ещё закрывается
    # Программа уже открыта: отдаём ей файл или ссылку и выходим
    # Автозапуск (--tray) при уже открытой программе ничего не показывает — просто выходим
    elif single_instance.send_to_running(args or (["--tray"] if "--tray" in sys.argv else ["--activate"])):
        logging.info(tr("Программа уже запущена — аргументы переданы открытому окну."))
        return 0
    import self_update
    self_update.cleanup_old()  # Старый .exe, оставшийся после обновления программы
    import crash_handler
    crash_handler.install()  # Необработанные ошибки — в окно с отчётом, а не молчаливое падение
    from main_window import ModUpdaterApp
    window = ModUpdaterApp()
    server = single_instance.InstanceServer(window)
    server.received.connect(window.handle_external)
    app.setQuitOnLastWindowClosed(False)  # Окно может прятаться в трей; выход — в closeEvent окна
    if "--tray" not in sys.argv or not window.start_in_tray():
        window.show()
    if args:
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(800, lambda: window.handle_external(args))
    return app.exec()


if __name__ == "__main__":
    if "--uninstall" in sys.argv:
        sys.exit(run_uninstall("--quiet" in sys.argv))
    if cli.wants_cli(sys.argv):
        sys.exit(cli.main())
    sys.exit(run_gui())
