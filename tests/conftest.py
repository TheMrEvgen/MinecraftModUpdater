import os
import sys

# Тесты запускаются из корня проекта: делаем модули приложения импортируемыми
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
