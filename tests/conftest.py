import os
import sys

# Тесты запускаются из корня проекта: делаем модули приложения импортируемыми
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Исходный язык строк — русский: тесты не зависят от языка, выбранного в настройках пользователя
os.environ["MODUPDATER_LANG"] = "ru"
os.environ["CURSEFORGE_API_KEY"] = ""  # Тесты без сети: CurseForge выключен


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _github_files(tmp_path, monkeypatch):
    """Связи модов с GitHub и ответы GitHub — во временной папке, а не в настройках пользователя."""
    import github_source
    monkeypatch.setattr(github_source, "LINKS_FILE", str(tmp_path / "github_links.json"))
    monkeypatch.setattr(github_source, "CACHE_FILE", str(tmp_path / "github_cache.json"))
