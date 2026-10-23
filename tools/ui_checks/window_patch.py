"""Подмена модальных диалогов (confirm, ask_choice, show_message…) во всех модулях главного окна для сценариев."""
import sys


def patch_window(**values):
    """Ставит values во все уже загруженные модули окна, где есть такие имена (main_window и window.*)."""
    patched = set()
    for name, module in list(sys.modules.items()):
        if name == "main_window" or name.startswith("window."):
            for attr, value in values.items():
                if hasattr(module, attr):
                    setattr(module, attr, value)
                    patched.add(attr)
    missing = set(values) - patched
    if missing:
        raise AttributeError(f"Ни в одном модуле окна нет: {', '.join(sorted(missing))}")
