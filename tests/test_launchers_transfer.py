"""Сборки в разных лаунчерах: Forge/NeoForge установщиком, Modrinth App, архив CurseForge, перенос и удаление (без сети)."""
import json
import os
import sqlite3
import subprocess
import zipfile

import pytest

import exporters
import java_check
import maintenance
import modpack_install
import modrinth_app
import official_launcher


# --- Forge / NeoForge в Minecraft Launcher ---

def test_forge_version_ids():
    assert official_launcher.version_id("forge", "1.20.1", "1.20.1-47.2.0") == "1.20.1-forge-47.2.0"
    assert official_launcher.version_id("forge", "1.20.1", "47.2.0") == "1.20.1-forge-47.2.0"
    assert official_launcher.version_id("neoforge", "1.21.1", "21.1.77") == "neoforge-21.1.77"
    assert official_launcher.version_id("fabric", "1.21.1", "0.16.5") == "fabric-loader-0.16.5-1.21.1"
    assert all(official_launcher.can_install(l) for l in ("fabric", "quilt", "forge", "neoforge"))


def _fake_installer(monkeypatch, created_id, returncode=0, calls=None):
    monkeypatch.setattr(java_check, "find_java", lambda *a: "C:/java/bin/java.exe")
    monkeypatch.setattr("server_pack._download", lambda url, target, session=None: open(target, "wb").close())

    def run(cmd, **kwargs):
        if calls is not None:
            calls.append(cmd)
        mc_dir = cmd[-1]
        assert os.path.isfile(os.path.join(mc_dir, "launcher_profiles.json"))  # Без него установщик не ставит
        if returncode == 0:
            folder = os.path.join(mc_dir, "versions", created_id)
            os.makedirs(folder)
            open(os.path.join(folder, f"{created_id}.json"), "w").write("{}")
        return subprocess.CompletedProcess(cmd, returncode, "done", "boom" if returncode else "")

    monkeypatch.setattr(subprocess, "run", run)


def test_forge_installed_by_its_installer(tmp_path, monkeypatch):
    calls = []
    _fake_installer(monkeypatch, "1.20.1-forge-47.2.0", calls=calls)
    vid = official_launcher.install_loader(str(tmp_path), "forge", "1.20.1", "1.20.1-47.2.0")
    assert vid == "1.20.1-forge-47.2.0"
    assert calls[0][1:3] == ["-jar", calls[0][2]] and "--installClient" in calls[0]
    # Повторно установщик не запускается
    assert official_launcher.install_loader(str(tmp_path), "forge", "1.20.1", "47.2.0") == vid and len(calls) == 1


def test_neoforge_unusual_folder_name_and_errors(tmp_path, monkeypatch):
    _fake_installer(monkeypatch, "neoforge-21.1.77-custom")  # Новая папка с именем загрузчика тоже подходит
    assert official_launcher.install_loader(str(tmp_path / "a"), "neoforge", "1.21.1", "21.1.77") == \
        "neoforge-21.1.77-custom"
    _fake_installer(monkeypatch, "x", returncode=1)
    with pytest.raises(ValueError, match="boom"):
        official_launcher.install_loader(str(tmp_path / "b"), "forge", "1.20.1", "47.2.0")
    monkeypatch.setattr(java_check, "find_java", lambda *a: None)
    with pytest.raises(ValueError, match="Java 17"):
        official_launcher.install_loader(str(tmp_path / "c"), "forge", "1.20.1", "47.2.0")


def test_find_java_picks_suitable_version(monkeypatch):
    versions = {"j8": 8, "j17": 17, "j21": 21}
    monkeypatch.setattr(java_check, "java_candidates", lambda: list(versions))
    monkeypatch.setattr(java_check, "java_version_of", versions.get)
    assert java_check.find_java(17) == "j17"
    assert java_check.find_java(8, 8) == "j8"
    assert java_check.find_java(25) is None


def test_remove_installation(tmp_path):
    key, _ = official_launcher.add_installation(str(tmp_path), "Пак", "fabric-loader-0.16.5-1.21.1", str(tmp_path / "g"))
    assert official_launcher.remove_installation(str(tmp_path), key)
    assert key not in official_launcher.read_profiles(str(tmp_path))["profiles"]
    assert official_launcher.remove_installation(str(tmp_path), key) is None


# --- Modrinth App ---

SCHEMA = """CREATE TABLE profiles (path TEXT NOT NULL PRIMARY KEY, install_stage TEXT NOT NULL, name TEXT NOT NULL,
    icon_path TEXT NULL, game_version TEXT NOT NULL, mod_loader TEXT NOT NULL, mod_loader_version TEXT NULL,
    groups JSONB NOT NULL, created INTEGER NOT NULL, modified INTEGER NOT NULL,
    submitted_time_played INTEGER NOT NULL DEFAULT 0, override_extra_launch_args JSONB NOT NULL,
    override_custom_env_vars JSONB NOT NULL, launcher_feature_version TEXT NOT NULL)"""


def _modrinth_db(root, template=True):
    os.makedirs(root / "profiles", exist_ok=True)
    conn = sqlite3.connect(root / "app.db")
    conn.execute(SCHEMA)
    if template:
        conn.execute("INSERT INTO profiles VALUES ('old', 'installed', 'Old', NULL, '1.20.1', 'fabric', '0.15', '[]', "
                     "1, 1, 0, '[]', '[]', 'v3')")
    conn.commit()
    conn.close()


def test_modrinth_app_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(modrinth_app, "running", lambda: False)
    _modrinth_db(tmp_path)
    game = tmp_path / "profiles" / "Мой пак"
    game.mkdir()
    modrinth_app.add_profile(str(tmp_path), str(game), "Мой пак", "1.20.1", "forge", "47.2.0")
    conn = sqlite3.connect(tmp_path / "app.db")
    row = conn.execute("SELECT name, install_stage, mod_loader, mod_loader_version, launcher_feature_version "
                       "FROM profiles WHERE path = 'Мой пак'").fetchone()
    assert row == ("Мой пак", "not_installed", "forge", "1.20.1-47.2.0", "v3")  # Незнакомый столбец — как у старого
    conn.close()
    from launchers import find_modrinth_instances
    found = {i.name: i for i in find_modrinth_instances(str(tmp_path))}
    assert found["Мой пак"].game_version == "1.20.1" and found["Мой пак"].loader == "forge"
    modrinth_app.remove_profile(str(tmp_path), str(game))
    assert sqlite3.connect(tmp_path / "app.db").execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 1


def test_modrinth_app_refuses_when_open_or_unknown(tmp_path, monkeypatch):
    _modrinth_db(tmp_path, template=False)
    monkeypatch.setattr(modrinth_app, "running", lambda: False)
    with pytest.raises(ValueError, match="создайте"):  # Незнакомый обязательный столбец и не с кого взять
        modrinth_app.check(str(tmp_path))
    monkeypatch.setattr(modrinth_app, "running", lambda: True)
    with pytest.raises(ValueError, match="Закройте"):
        modrinth_app.check(str(tmp_path))


# --- Архив CurseForge ---

def test_curseforge_export(tmp_path, monkeypatch):
    import curseforge
    monkeypatch.setattr(curseforge, "available", lambda: False)
    game = tmp_path / "game"
    (game / "mods").mkdir(parents=True)
    (game / "config").mkdir()
    (game / "config" / "a.toml").write_text("a=1")
    for name in ("cf.jar", "mr.jar", "off.jar.disabled"):
        (game / "mods" / name).write_bytes(name.encode())
    entries = [
        {"filepath": str(game / "mods" / "cf.jar"), "project_id": "cf:100", "installed_info": {"cf_file_id": 555}},
        {"filepath": str(game / "mods" / "mr.jar"), "project_id": "AANobbMI", "installed_info": {}},
    ]
    path = tmp_path / "pack.zip"
    assert exporters.build_curseforge_export(str(path), entries, str(game), "Пак", "1.20.1", "forge",
                                             "1.20.1-47.2.0") == (1, 1)
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read("manifest.json"))
        names = sorted(z.namelist())
    assert manifest["minecraft"]["modLoaders"] == [{"id": "forge-47.2.0", "primary": True}]
    assert manifest["files"] == [{"projectID": 100, "fileID": 555, "required": True}]
    assert names == ["manifest.json", "overrides/config/a.toml", "overrides/mods/mr.jar"]
    # Такой архив читается как сборка CurseForge
    from scanner import read_curseforge_pack
    assert read_curseforge_pack(str(path))["name"] == "Пак"


def test_install_for_curseforge_app(tmp_path, monkeypatch):
    """CurseForge App: сборка Modrinth качается во временную папку и упаковывается в архив для импорта."""
    import config as app_config
    import hashlib
    monkeypatch.setattr(app_config, "CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(modpack_install, "downloads_folder", lambda: str(tmp_path / "dl"))
    data = b"mod"
    archive = tmp_path / "p.mrpack"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("modrinth.index.json", json.dumps({
            "formatVersion": 1, "game": "minecraft", "versionId": "1", "name": "Пак",
            "dependencies": {"minecraft": "1.21.1", "fabric-loader": "0.16.5"},
            "files": [{"path": "mods/a.jar", "downloads": ["https://cdn.modrinth.com/a.jar"],
                       "hashes": {"sha1": hashlib.sha1(data).hexdigest()}, "fileSize": 3}]}))
        z.writestr("overrides/config/c.toml", "c=1")
    from test_modpack_install import FakeSession
    plan = modpack_install.load_plan(str(archive))
    target = modpack_install.make_target("curseforge", "", "Пак")
    assert modpack_install.install(plan, target, session=FakeSession({"https://cdn.modrinth.com/a.jar": data})) == []
    assert not os.path.exists(target.game_dir)  # Временная папка убрана
    with zipfile.ZipFile(target.curseforge_zip) as z:
        assert sorted(z.namelist()) == ["manifest.json", "overrides/config/c.toml", "overrides/mods/a.jar"]


# --- Цели установки и перенос ---

def test_targets_for_each_launcher(tmp_path):
    prism = modpack_install.make_target("prism", str(tmp_path / "inst"), "Пак")
    assert prism.instance_dir and prism.game_dir.endswith(os.path.join("Пак", "minecraft"))
    assert modpack_install.make_target("multimc", str(tmp_path / "mmc"), "Пак").source == "multimc"
    official = modpack_install.make_target("official", str(tmp_path / ".minecraft"), "Пак")
    assert official.minecraft_dir and os.path.join("modpacks", "Пак") in official.game_dir
    modrinth = modpack_install.make_target("modrinth", str(tmp_path / "mr"), "Пак")
    assert modrinth.modrinth_root and modrinth.game_dir == str(tmp_path / "mr" / "profiles" / "Пак")


def test_folder_install_puts_loader_into_versions(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(official_launcher, "install_loader",
                        lambda d, *a, **k: calls.append(d) or "fabric-loader-0.16.5-1.21.1")
    plan = modpack_install.versions_plan("Пак", "1.21.1", "fabric", "0.16.5")
    target = modpack_install.folder_target(str(tmp_path / "tl"), "Пак", own_folder=False, loader_here=True)
    modpack_install.install(plan, target)
    assert calls == [str(tmp_path / "tl")] and target.version_id == "fabric-loader-0.16.5-1.21.1"
    assert not target.launcher_id  # Это не Minecraft Launcher — установку не добавляем
    (tmp_path / "tl" / "versions").mkdir()
    assert modpack_install.looks_like_launcher_dir(str(tmp_path / "tl"))


def test_transfer_to_minecraft_launcher(tmp_path, monkeypatch):
    monkeypatch.setattr(official_launcher, "install_loader", lambda *a, **k: "fabric-loader-0.16.5-1.21.1")
    old = tmp_path / "prism" / "Пак" / "minecraft"
    (old / "mods").mkdir(parents=True)
    (old / "mods" / "a.jar").write_bytes(b"a")
    mc = tmp_path / ".minecraft"
    target = modpack_install.transfer(str(old), "official", str(mc), "Пак", "1.21.1", "fabric", "0.16.5")
    assert (tmp_path / ".minecraft" / "modpacks" / "Пак" / "mods" / "a.jar").exists() and not (old / "mods").exists()
    installation = official_launcher.read_profiles(str(mc))["profiles"][target.launcher_id]
    assert installation["lastVersionId"] == "fabric-loader-0.16.5-1.21.1"
    assert os.path.normpath(installation["gameDir"]) == os.path.normpath(target.game_dir)


def test_transfer_failure_cleans_new_folder(tmp_path, monkeypatch):
    def broken(*a, **k):
        raise ValueError("нет сети")
    monkeypatch.setattr(official_launcher, "install_loader", broken)
    old = tmp_path / "old"
    (old / "mods").mkdir(parents=True)
    with pytest.raises(ValueError):
        modpack_install.transfer(str(old), "official", str(tmp_path / "mc"), "Пак", "1.21.1", "fabric", "0.16.5")
    assert (old / "mods").exists() and not (tmp_path / "mc" / "modpacks" / "Пак").exists()


def test_plan_size_and_free_space(tmp_path):
    plan = modpack_install.versions_plan("Пак", "1.21.1", "fabric", "0.16.5")
    plan.files = [modpack_install.PackFile("mods/a.jar", "u", size=100),
                  modpack_install.PackFile("saves/w.zip", "u", size=50, world=True)]
    assert plan.size() == 200
    assert modpack_install.free_space(str(tmp_path / "нет" / "такой")) > 0


# --- Удаление сборки с диска ---

def test_deletion_plan(tmp_path, monkeypatch):
    instance = tmp_path / "instances" / "Пак"
    (instance / "minecraft").mkdir(parents=True)
    (instance / "mmc-pack.json").write_text("{}")
    assert maintenance.deletion_plan({"mods_path": str(instance / "minecraft"), "source": "prism"}).paths == \
        [str(instance)]  # Экземпляр Prism — целиком
    shared = tmp_path / "legacy"
    (shared / "versions").mkdir(parents=True)
    with pytest.raises(ValueError, match="общая папка"):
        maintenance.deletion_plan({"mods_path": str(shared), "source": "legacy"})
    with pytest.raises(ValueError):
        maintenance.deletion_plan({"mods_path": os.path.expanduser("~"), "source": "manual"})
    own = tmp_path / ".minecraft" / "modpacks" / "Пак"
    own.mkdir(parents=True)
    deletion = maintenance.deletion_plan({"mods_path": str(own), "source": "official", "launcher_id": "k"})
    assert deletion.paths == [str(own)] and deletion.official_key == "k"


def test_delete_pack_removes_launcher_entry(tmp_path, monkeypatch):
    removed = []
    monkeypatch.setattr(maintenance, "to_recycle_bin", removed.append)
    monkeypatch.setattr(official_launcher, "minecraft_dir", lambda: str(tmp_path))
    key, _ = official_launcher.add_installation(str(tmp_path), "Пак", "v", str(tmp_path / "g"))
    maintenance.delete_pack(maintenance.Deletion([str(tmp_path / "g")], official_key=key))
    assert removed == [str(tmp_path / "g")] and not official_launcher.read_profiles(str(tmp_path))["profiles"]
