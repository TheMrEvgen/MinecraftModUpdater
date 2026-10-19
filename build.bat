@echo off
echo Установка зависимостей из requirements.txt...
pip install -r requirements.txt

echo.
echo Запуск сборки проекта в .exe и ZIP...
python build.py

echo.
echo Сборка завершена.
pause