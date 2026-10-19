import PyInstaller.__main__
import os
import re
import shutil
import zipfile
import datetime

# --- НАСТРОЙКИ ---
APP_NAME = "Minecraft Mod Updater beta"
VERSION = "3.0.0-beta"  # Версия продукта. Может содержать текст.
COPYRIGHT_INFO = f"Copyright (c) {datetime.datetime.now().year} TheMrEvgen" # Заменено © на (c) для совместимости
COMPANY_NAME = "TheMrEvgen" # Укажите название компании
# -----------------

ENTRY_POINT = "main.py"
ICON_FILE = "icon.ico"
VERSION_FILE = "file_version_info.txt"

def create_version_file():
    """Создает файл с информацией о версии для PyInstaller."""
    print(f"--- Создание файла версии: {VERSION_FILE} ---")
    
    # Извлекаем только цифры из версии для filevers и prodvers.
    # PyInstaller ожидает здесь кортеж из 4-х целых чисел.
    version_numbers = re.findall(r'\d+', VERSION)
    # Дополняем нулями до 4-х компонентов (major, minor, build, revision)
    while len(version_numbers) < 4:
        version_numbers.append('0')
    
    # Оставляем только первые 4 компонента и соединяем их через запятую
    file_version_tuple = ','.join(version_numbers[:4])

    # Используем обычную строку и единый вызов .format() для всех подстановок.
    # Это избегает смешивания методов форматирования (f-string и .format) и является более надежным.
    version_info_content = """
# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({file_version_tuple}),
    prodvers=({file_version_tuple}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
      StringTable(
        u'040904B0',
        [StringStruct(u'CompanyName', u'{COMPANY_NAME}'),
        StringStruct(u'FileDescription', u'{APP_NAME}'),
        StringStruct(u'FileVersion', u'{VERSION}'),
        StringStruct(u'InternalName', u'ModUpdater'),
        StringStruct(u'LegalCopyright', u'{COPYRIGHT_INFO}'),
        StringStruct(u'OriginalFilename', u'{APP_NAME}.exe'),
        StringStruct(u'ProductName', u'{APP_NAME}'),
        StringStruct(u'ProductVersion', u'{VERSION}')])
      ]), 
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
"""
    with open(VERSION_FILE, "w", encoding="utf-8-sig") as f:
        f.write(version_info_content.format(
            file_version_tuple=file_version_tuple,
            APP_NAME=APP_NAME,
            VERSION=VERSION,
            COPYRIGHT_INFO=COPYRIGHT_INFO,
            COMPANY_NAME=COMPANY_NAME
        ))
    print("--- Файл версии успешно создан ---")

# Модули Qt, которые приложение не использует: исключаем, чтобы уменьшить размер сборки
EXCLUDED_MODULES = [
    "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.QtQml", "PyQt6.QtQuick",
    "PyQt6.QtMultimedia", "PyQt6.QtNetwork", "PyQt6.QtSql", "PyQt6.QtBluetooth",
    "PyQt6.QtDesigner", "PyQt6.QtOpenGL", "PyQt6.QtOpenGLWidgets", "tkinter", "unittest", "pytest",
]


def build_executable(onedir: bool = False):
    """
    Запускает PyInstaller для сборки .exe файла.
    onedir=True собирает папку вместо одного файла: запуск быстрее (exe не распаковывается
    во временную папку при каждом старте) и реже бывают ложные срабатывания антивирусов.
    """
    print("--- Начало сборки .exe ---")
    params = [
        ENTRY_POINT,
        '--name', APP_NAME,
        '--onedir' if onedir else '--onefile',
        '--windowed',     # Запускать без консольного окна (консольный режим подключается к консоли сам)
        '--clean',        # Очистить кэш PyInstaller перед сборкой
        '--noconfirm',
        '--version-file', VERSION_FILE # Добавляем информацию о версии
    ]
    for module in EXCLUDED_MODULES:
        params.extend(['--exclude-module', module])

    if os.path.exists(ICON_FILE):
        params.extend(['--icon', ICON_FILE])
        print(f"Используется иконка: {ICON_FILE}")
    else:
        print(f"ПРЕДУПРЕЖДЕНИЕ: Файл иконки '{ICON_FILE}' не найден. Приложение будет без иконки.")

    try:
        PyInstaller.__main__.run(params)
        print("\n--- Сборка .exe успешно завершена! ---")
        return True
    except Exception as e:
        print(f"\n--- ОШИБКА СБОРКИ .exe: {e} ---")
        return False

def create_zip_archive(onedir: bool = False):
    """Создает ZIP-архив с готовым .exe файлом (или папкой сборки в режиме onedir)."""
    dist_path = 'dist'
    exe_name = f"{APP_NAME}.exe"
    source_path = os.path.join(dist_path, APP_NAME) if onedir else os.path.join(dist_path, exe_name)

    if not os.path.exists(source_path):
        print(f"Не найден результат сборки '{source_path}'. Архив не будет создан.")
        return

    print("\n--- Начало создания ZIP-архива ---")
    # Создаем "безопасное" имя файла, заменяя пробелы и другие символы
    safe_version_str = re.sub(r'[^\w.-]', '_', VERSION)
    zip_filename = f"{APP_NAME}_v{safe_version_str}.zip"

    try:
        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zf:
            if onedir:
                for root, _, files in os.walk(source_path):
                    for name in files:
                        full_path = os.path.join(root, name)
                        zf.write(full_path, os.path.relpath(full_path, dist_path))
            else:
                zf.write(source_path, exe_name)
        print(f"Архив '{zip_filename}' успешно создан в корневой папке проекта.")
        print("--- Создание ZIP-архива завершено! ---")
    except Exception as e:
        print(f"--- ОШИБКА СОЗДАНИЯ АРХИВА: {e} ---")

def cleanup():
    """Удаляет временные файлы и папки, созданные PyInstaller."""
    print("\n--- Очистка временных файлов ---")
    if os.path.isdir('build'):
        shutil.rmtree('build')
        print("Удалена папка 'build'")
    spec_file = f"{APP_NAME}.spec"
    if os.path.exists(spec_file):
        os.remove(spec_file)
        print(f"Удален файл '{spec_file}'")
    if os.path.exists(VERSION_FILE):
        os.remove(VERSION_FILE)
        print(f"Удален файл '{VERSION_FILE}'")
    print("--- Очистка завершена ---")

if __name__ == '__main__':
    import sys
    onedir_mode = '--onedir' in sys.argv
    create_version_file()
    if build_executable(onedir_mode):
        create_zip_archive(onedir_mode)
    cleanup()