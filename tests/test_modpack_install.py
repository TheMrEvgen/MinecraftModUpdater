"""Установка готовой сборки: чтение .mrpack, overrides, защита путей, экземпляр Prism, проверка хэшей (без сети)."""
import hashlib
import json
import os
import zipfile

import modpack_install


def make_mrpack(path, files, overrides):
    index = {"formatVersion": 1, "game": "minecraft", "versionId": "1.0", "name": "Пак",
             "dependencies": {"minecraft": "1.21.1", "fabric-loader": "0.16.5"}, "files": files}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("modrinth.index.json", json.dumps(index))
        for name, data in overrides.items():
            z.writestr(name, data)


class FakeSession:
    def __init__(self, contents):
        self.contents = contents

    def get(self, url, timeout=None):
        session = self

        class Response:
            content = session.contents.get(url, b"")

            def raise_for_status(self):
                pass

            def json(self):
                return {"requires": [{"uid": "org.lwjgl3", "suggests": "3.3.3"}]}

        return Response()


def test_mrpack_plan_and_install(tmp_path, monkeypatch):
    good = b"mod bytes"
    files = [
        {"path": "mods/good.jar", "downloads": ["https://cdn.modrinth.com/good.jar"],
         "hashes": {"sha1": hashlib.sha1(good).hexdigest()}, "fileSize": len(good)},
        {"path": "mods/broken.jar", "downloads": ["https://cdn.modrinth.com/broken.jar"], "hashes": {"sha1": "0" * 40}},
        {"path": "mods/server-only.jar", "downloads": ["https://x/s.jar"], "env": {"client": "unsupported"}},
        {"path": "../evil.jar", "downloads": ["https://x/evil.jar"]},
    ]
    archive = tmp_path / "pack.mrpack"
    make_mrpack(archive, files, {"overrides/config/mod.toml": "a=1", "client-overrides/options.txt": "fov:90",
                                 "overrides/../../escape.txt": "nope"})
    plan = modpack_install.load_plan(str(archive))
    assert (plan.name, plan.game_version, plan.loader, plan.loader_version) == ("Пак", "1.21.1", "fabric", "0.16.5")
    assert [f.path for f in plan.files] == ["mods/good.jar", "mods/broken.jar"]  # Серверный и «за пределы» — пропущены

    session = FakeSession({"https://cdn.modrinth.com/good.jar": good, "https://cdn.modrinth.com/broken.jar": b"x",
                           "https://meta.prismlauncher.org/v1/net.minecraft/1.21.1.json": b""})
    monkeypatch.setattr("mod_updater_logic.SESSION", session)
    target = modpack_install.prism_target(str(tmp_path / "instances"), "Пак")
    errors = modpack_install.install(plan, target, session=session)
    game = target.game_dir
    assert open(os.path.join(game, "mods", "good.jar"), "rb").read() == good
    assert not os.path.exists(os.path.join(game, "mods", "broken.jar")) and any("broken.jar" in e for e in errors)
    assert open(os.path.join(game, "config", "mod.toml")).read() == "a=1"
    assert open(os.path.join(game, "options.txt")).read() == "fov:90"
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "instances" / "escape.txt").exists()
    pack = json.load(open(os.path.join(target.instance_dir, "mmc-pack.json"), encoding="utf-8"))
    assert [(c["uid"], c["version"]) for c in pack["components"]] == [
        ("org.lwjgl3", "3.3.3"), ("net.minecraft", "1.21.1"), ("net.fabricmc.intermediary", "1.21.1"),
        ("net.fabricmc.fabric-loader", "0.16.5")]
    assert "InstanceType=OneSix" in open(os.path.join(target.instance_dir, "instance.cfg"), encoding="utf-8").read()


def test_unique_folders(tmp_path):
    first = modpack_install.folder_target(str(tmp_path), "Пак: лучший?")
    os.makedirs(first.game_dir)
    second = modpack_install.folder_target(str(tmp_path), "Пак: лучший?")
    assert os.path.basename(first.game_dir) == "Пак_ лучший_" and second.game_dir.endswith("(2)")
