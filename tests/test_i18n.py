"""Перевод интерфейса: каталог полный, подстановки совпадают, plural и tr работают на обоих языках."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import i18n
import i18n_check


def test_english_catalog_is_complete():
    report = i18n_check.problems("en")
    assert not report["missing"], report["missing"][:10]
    assert not report["placeholders"], report["placeholders"][:10]


def test_tr_and_plural_in_both_languages():
    try:
        i18n.set_language("en")
        assert i18n.tr("Обновить все") == "Update all"
        assert i18n.tr("Найдено новых сборок: {0}", 3) == "New modpacks found: 3"
        assert i18n.tr("строка без перевода") == "строка без перевода"
        assert i18n.plural(1, "мод", "мода", "модов") == "mod"
        assert i18n.plural(5, "мод", "мода", "модов") == "mods"
        i18n.set_language("ru")
        assert i18n.tr("Обновить все") == "Обновить все"
        assert i18n.plural(2, "мод", "мода", "модов") == "мода"
        assert i18n.plural(11, "мод", "мода", "модов") == "модов"
    finally:
        i18n.set_language("ru")


def test_dates_follow_language():
    from mod_updater_logic import format_date
    try:
        i18n.set_language("ru")
        assert format_date("2026-09-06T12:00:00Z") == "06.09.2026"
        i18n.set_language("en")
        assert format_date("2026-09-06T12:00:00Z") == "2026-09-06"
        assert i18n.tr("Сканирование прервано пользователем.") == "Scan cancelled by the user."
    finally:
        i18n.set_language("ru")
