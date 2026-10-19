"""Новые возможности: типы контента, выключение модов, дубликаты, снимки, конфликты, закрепление, метки."""
import json
import os
import zipfile

from content import MODS, RESOURCEPACKS, SHADERS, content_by_key, enabled_name, is_disabled
from enums import ModStatus


def _jar(path, mod_id="m", name="Mod", version="1.0"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("fabric.mod.json", json.dumps({"id": mod_id, "name": name, "version": version}))
    return str(path)


def test_content_types():
    assert MODS.matches("a.jar") and MODS.matches("a.jar.disabled") and not MODS.matches("a.zip")
    assert SHADERS.matches("BSL.zip") and RESOURCEPACKS.matches("pack.zip.disabled")
    assert SHADERS.loaders("fabric") == ["iris", "optifine", "canvas", "vanilla"]
    assert RESOURCEPACKS.loaders("fabric") == ["minecraft"] and MODS.loaders("fabric") == ["fabric"]
    assert content_by_key("nope") is MODS and content_by_key("shaders") is SHADERS
    assert is_disabled("x.jar.disabled") and enabled_name("x.jar.disabled") == "x.jar"


def test_set_disabled_and_scan_local(tmp_path):
    from scanner import scan_local
    from updater import set_disabled
    path = _jar(tmp_path / "a.jar", name="Alpha")
    _jar(tmp_path / "b.jar", name="Beta")
    new_path = set_disabled(path, True)
    assert new_path.endswith("a.jar.disabled") and not os.path.exists(path)
    entries = {e["name"]: e for e in scan_local(str(tmp_path), None)}
    assert entries["Alpha"]["disabled"] is True and entries["Beta"]["disabled"] is False
    assert entries["Alpha"]["size"] > 0
    assert set_disabled(new_path, False) == path and os.path.exists(path)


def test_install_keeps_disabled_suffix(tmp_path, monkeypatch):
    import updater
    from updater import UpdateTransaction, install_mod
    old = _jar(tmp_path / "a-1.jar.disabled")

    def fake_download(url, dest, **kwargs):
        with open(dest, "wb") as f:
            f.write(b"new")
    monkeypatch.setattr(updater, "download_file", fake_download)
    mod = {"name": "A", "filepath": old, "disabled": True, "new_filename": "a-2.jar", "download_url": "x"}
    assert install_mod(mod, str(tmp_path), UpdateTransaction(str(tmp_path))) == ModStatus.UPDATED
    assert os.path.exists(tmp_path / "a-2.jar.disabled") and not os.path.exists(old)


def test_scan_reports_duplicates(tmp_path, monkeypatch):
    import scanner
    _jar(tmp_path / "sodium-old.jar", version="0.5")
    _jar(tmp_path / "sodium-new.jar", version="0.6")
    by_name = {}
    for f in os.listdir(tmp_path):
        from mod_updater_logic import get_file_hash
        by_name[f] = get_file_hash(str(tmp_path / f))
    infos = {by_name["sodium-old.jar"]: {"project_id": "AANobbMI", "version_number": "0.5", "date_published": "2024-01-01T00:00:00Z"},
             by_name["sodium-new.jar"]: {"project_id": "AANobbMI", "version_number": "0.6", "date_published": "2025-01-01T00:00:00Z"}}
    monkeypatch.setattr(scanner, "get_mods_info_from_hashes", lambda hashes: {h: infos[h] for h in hashes})
    monkeypatch.setattr(scanner, "get_latest_versions_from_hashes", lambda *a, **k: {})
    monkeypatch.setattr(scanner, "get_projects_info_from_ids",
                        lambda ids: {"AANobbMI": {"title": "Sodium", "slug": "sodium", "categories": ["optimization", "fabric"]}})
    data = scanner.scan_mods(str(tmp_path), "1.21.1", "fabric", None)
    main = next(e for e in data if not e.get("duplicate_of"))
    extra = next(e for e in data if e.get("duplicate_of"))
    assert main["current_version"] == "0.6" and main["duplicates"] == ["sodium-old.jar"]
    assert extra["duplicate_of"] == "sodium-new.jar" and main["categories"] == ["optimization", "fabric"]

    from view_model import CAT_PROBLEMS, build_view_items, matches_category
    items = build_view_items(data)
    assert len(items) == 2  # Копия не схлопывается с основным модом
    assert all(matches_category(e, CAT_PROBLEMS) for e in items)


def test_snapshots_create_restore_and_prune(tmp_path):
    from updater import create_snapshot, list_snapshots, prune_snapshots, restore_snapshot, rollback_transaction
    a = _jar(tmp_path / "a.jar")
    snap = create_snapshot(str(tmp_path), "update_all")
    assert snap and [f["name"] for f in snap["files"]] == ["a.jar"]
    os.remove(a)
    _jar(tmp_path / "b.jar")
    transaction, errors = restore_snapshot(str(tmp_path), list_snapshots(str(tmp_path))[0])
    assert not errors and sorted(f for f in os.listdir(tmp_path) if f.endswith(".jar")) == ["a.jar"]
    # Восстановление отменяется как обычная операция
    manifest = dict(transaction.manifest, dir=transaction.dir)
    assert rollback_transaction(str(tmp_path), manifest) == []
    assert sorted(f for f in os.listdir(tmp_path) if f.endswith(".jar")) == ["b.jar"]

    create_snapshot(str(tmp_path), "manual")
    for _ in range(3):
        create_snapshot(str(tmp_path), "update_all")
    prune_snapshots(str(tmp_path), keep=1)
    reasons = [s["reason"] for s in list_snapshots(str(tmp_path))]
    assert reasons.count("manual") == 1 and reasons.count("update_all") == 1


def test_known_conflicts_and_pins():
    from view_model import (
        CAT_UPDATES, build_view_items, group_of, is_updatable, matches_category, pin_key, status_style,
    )
    sodium = {"name": "Sodium", "slug": "sodium", "project_id": "s", "filepath": "mods/sodium.jar",
              "status": ModStatus.UPDATE_AVAILABLE, "download_url": "u"}
    optifine = {"name": "OptiFine", "filepath": "mods/OptiFine_1.21.jar", "status": ModStatus.NOT_FOUND}
    items = build_view_items([sodium, optifine])
    assert "несовместим с OptiFine" in items[0]["_issues"] and "несовместим с Sodium" in items[1]["_issues"]

    optifine["disabled"] = True
    items = build_view_items([sodium, optifine])
    assert not items[0]["_issues"] and group_of(optifine) == "disabled"

    sodium["_pinned"] = True
    assert not is_updatable(sodium) and not matches_category(sodium, CAT_UPDATES)
    assert status_style(sodium)[0] == "Закреплён" and group_of(sodium) == "ok"
    assert pin_key(sodium) == "s" and pin_key({"filepath": "mods/x.jar.disabled"}) == "x.jar"


def test_tags_and_sizes():
    from view_model import collect_tags, entry_tags, format_size, search_text
    entry = {"project_id": "p", "categories": ["optimization", "fabric"], "client_side": "required",
             "server_side": "unsupported"}
    tags = entry_tags(entry, {"p": ["интерфейс"]})
    assert tags == [("cat:optimization", "оптимизация"), ("side:client", "только клиент"), ("user:интерфейс", "интерфейс")]
    assert collect_tags([entry, {"categories": ["optimization"]}])[0] == ("cat:optimization", "оптимизация", 2)
    assert "оптимизация" in search_text(entry)
    assert format_size(512) == "512 Б" and format_size(3 * 1024 * 1024) == "3,0 МБ"
