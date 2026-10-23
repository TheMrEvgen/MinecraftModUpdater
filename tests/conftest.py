import os
import sys

# Тесты запускаются из корня проекта: делаем модули приложения импортируемыми
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Исходный язык строк — русский: тесты не зависят от языка, выбранного в настройках пользователя
os.environ["MODUPDATER_LANG"] = "ru"
os.environ["CURSEFORGE_API_KEY"] = ""  # Тесты без сети: CurseForge выключен
