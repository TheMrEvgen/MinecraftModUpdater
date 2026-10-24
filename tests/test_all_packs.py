"""Все сборки: поиск мода, одинаковые файлы, объединение жёсткими ссылками (только по команде)."""
import os

import all_packs


def row(pack, name, path, sha1, size=100):
    return {"pack": pack, "name": name, "version": "1", "path": str(path), "sha1": sha1, "size": size}


def test_search_groups_by_mod_and_sorts_by_spread(tmp_path):
    rows = [row("A", "Sodium", tmp_path / "a" / "sodium.jar", "s"), row("B", "Sodium", tmp_path / "b" / "sodium.jar", "s"),
            row("A", "Lithium", tmp_path / "a" / "lithium.jar", "l")]
    result = all_packs.search(rows, "")
    assert [name for name, _ in result] == ["Sodium", "Lithium"]
    assert [name for name, _ in all_packs.search(rows, "lith")] == ["Lithium"]
    assert all_packs.search(rows, "нет такого") == []


def test_duplicates_and_linking(tmp_path):
    files = []
    for pack in ("A", "B", "C"):
        folder = tmp_path / pack
        folder.mkdir()
        path = folder / "sodium.jar"
        path.write_bytes(b"same content" * 100)
        files.append(row(pack, "Sodium", path, "sha-sodium", size=1200))
    other = tmp_path / "A" / "unique.jar"
    other.write_bytes(b"unique")
    rows = files + [row("A", "Unique", other, "sha-unique", 6)]
    groups = all_packs.duplicate_groups(rows)
    assert len(groups) == 1 and len(groups[0]) == 3
    assert all_packs.savings(groups) == 2400
    linked, freed, errors = all_packs.link_all(groups)
    assert (linked, freed, errors) == (2, 2400, [])
    inodes = {os.stat(r["path"]).st_ino for r in files}
    assert len(inodes) == 1  # Один файл на диске
    assert all((tmp_path / p / "sodium.jar").read_bytes() == b"same content" * 100 for p in "ABC")
    assert all_packs.duplicate_groups(rows) == []  # Повторно объединять нечего
    # Обновление в одной сборке (новый файл вместо старого) другие не затрагивает
    os.replace(tmp_path / "A" / "sodium.jar", tmp_path / "A" / "sodium.old")
    (tmp_path / "A" / "sodium.jar").write_bytes(b"new version")
    assert (tmp_path / "B" / "sodium.jar").read_bytes() == b"same content" * 100
