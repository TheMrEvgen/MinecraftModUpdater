import hashlib
import json
import os

import config as app_config
import updater
from enums import ModStatus
from updater import (
    UpdateTransaction, get_last_transaction, install_mod, move_all_to_backup, remove_mods, rollback_transaction,
)


def _fake_download(content: bytes):
    def fake(url, dest_path, expected_hashes=None, progress_callback=None, is_cancelled=None):
        with open(dest_path, "wb") as f:
            f.write(content)
    return fake


def test_install_and_rollback(tmp_path, monkeypatch):
    mods = tmp_path / "mods"
    mods.mkdir()
    old = mods / "mod-1.0.jar"
    old.write_bytes(b"old")
    monkeypatch.setattr(updater, "download_file", _fake_download(b"new"))

    tx = UpdateTransaction(str(mods), "update")
    mod = {"name": "Mod", "filepath": str(old), "new_filename": "mod-2.0.jar", "download_url": "x", "new_version": "2.0"}
    assert install_mod(mod, str(mods), tx) == ModStatus.UPDATED
    assert not old.exists() and (mods / "mod-2.0.jar").read_bytes() == b"new"

    manifest = get_last_transaction(str(mods))
    assert manifest["installed"] == ["mod-2.0.jar"]
    assert rollback_transaction(str(mods), manifest) == []
    assert old.read_bytes() == b"old" and not (mods / "mod-2.0.jar").exists()
    assert get_last_transaction(str(mods)) is None


def test_install_same_filename_and_rollback(tmp_path, monkeypatch):
    mods = tmp_path / "mods"
    mods.mkdir()
    (mods / "mod.jar").write_bytes(b"old")
    monkeypatch.setattr(updater, "download_file", _fake_download(b"new"))
    tx = UpdateTransaction(str(mods))
    mod = {"name": "Mod", "filepath": str(mods / "mod.jar"), "new_filename": "mod.jar", "download_url": "x"}
    assert install_mod(mod, str(mods), tx) == ModStatus.UPDATED
    assert (mods / "mod.jar").read_bytes() == b"new"
    rollback_transaction(str(mods), get_last_transaction(str(mods)))
    assert (mods / "mod.jar").read_bytes() == b"old"


def test_install_rejects_path_traversal(tmp_path, monkeypatch):
    mods = tmp_path / "mods"
    mods.mkdir()
    monkeypatch.setattr(updater, "download_file", _fake_download(b"x"))
    mod = {"name": "Evil", "filepath": None, "new_filename": "../../evil.jar", "download_url": "x"}
    assert install_mod(mod, str(mods), UpdateTransaction(str(mods))) == ModStatus.INSTALLED
    assert (mods / "evil.jar").exists() and not (tmp_path / "evil.jar").exists()


def test_hash_mismatch_keeps_old_file(tmp_path, monkeypatch):
    from mod_updater_logic import HashMismatchError
    mods = tmp_path / "mods"
    mods.mkdir()
    old = mods / "mod.jar"
    old.write_bytes(b"old")

    def failing(*args, **kwargs):
        raise HashMismatchError("bad hash")
    monkeypatch.setattr(updater, "download_file", failing)
    mod = {"name": "Mod", "filepath": str(old), "new_filename": "mod-2.jar", "download_url": "x"}
    assert install_mod(mod, str(mods), UpdateTransaction(str(mods))) == ModStatus.HASH_ERROR
    assert old.read_bytes() == b"old"


def test_verify_file_hashes(tmp_path):
    from mod_updater_logic import verify_file_hashes
    f = tmp_path / "a.jar"
    f.write_bytes(b"data")
    good = {"sha512": hashlib.sha512(b"data").hexdigest(), "sha1": "wrong"}
    assert verify_file_hashes(str(f), good)  # sha512 приоритетнее
    assert not verify_file_hashes(str(f), {"sha1": "0" * 40})
    assert verify_file_hashes(str(f), None)


def test_delete_and_replace_all_rollback(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    for name in ("a.jar", "b.jar"):
        (mods / name).write_bytes(name.encode())
    (mods / "config-folder").mkdir()

    removed, errors = remove_mods([str(mods / "a.jar")], UpdateTransaction(str(mods), "delete"))
    assert removed and not errors and not (mods / "a.jar").exists()
    rollback_transaction(str(mods), get_last_transaction(str(mods)))
    assert (mods / "a.jar").exists()

    move_all_to_backup(str(mods), UpdateTransaction(str(mods), "replace_all"))
    assert sorted(os.listdir(mods)) == [updater.BACKUP_DIR_NAME]
    rollback_transaction(str(mods), get_last_transaction(str(mods)))
    assert {"a.jar", "b.jar", "config-folder"} <= set(os.listdir(mods))


def test_find_locked_files_on_unlocked_file(tmp_path):
    f = tmp_path / "a.jar"
    f.write_bytes(b"x")
    assert updater.find_locked_files([str(f)]) == []


def test_find_locked_files_detects_open_file(tmp_path):
    import sys
    if sys.platform != "win32":
        return
    f = tmp_path / "a.jar"
    f.write_bytes(b"x")
    # Python открывает файлы с FILE_SHARE_READ|WRITE, но эксклюзивное открытие всё равно невозможно
    with open(f, "rb"):
        assert updater.find_locked_files([str(f)]) == [str(f)]


def test_config_roundtrip_and_migration(tmp_path):
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"mods_path": "C:/game", "loader": "forge", "theme": "dark"}, f)
    cfg = app_config.load_config(path)
    assert cfg["profiles"]["Default"]["mods_path"] == "C:/game"
    assert cfg["profiles"]["Default"]["channel"] == app_config.DEFAULT_CHANNEL.value
    cfg["profiles"]["Профиль"] = app_config.default_profile()
    app_config.save_config(cfg, path)
    assert "Профиль" in app_config.load_config(path)["profiles"]


def test_config_corrupted(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{broken", encoding="utf-8")
    assert app_config.load_config(str(path))["current_profile"] == "Default"
