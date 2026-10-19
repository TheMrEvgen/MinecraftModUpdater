@echo off
chcp 65001 >nul
echo Установка зависимостей из requirements-dev.txt...
pip install -r requirements-dev.txt || goto :error

echo.
echo Запуск тестов...
python -m pytest -q || goto :error

echo.
echo Запуск сборки проекта в .exe и ZIP...
rem Для сборки папкой (быстрее запуск): build.bat --onedir
python build.py %* || goto :error

echo.
echo Сборка завершена.
pause
exit /b 0

:error
echo.
echo Сборка прервана из-за ошибки.
pause
exit /b 1
