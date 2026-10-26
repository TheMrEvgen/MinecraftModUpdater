"""Официальный Minecraft Launcher: установки из launcher_profiles.json, Fabric/Quilt в versions, правка и откат."""
import json
import os

import launcher_pack
import launchers
import official_launcher
from game_detection import extract_loader, extract_loader_version


class FakeSession:
    def __init__(self):
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"id": "fabric-loader-x", "inheritsFrom": "1.21.1", "mainClass": "net.fabricmc.Knot",
                        "libraries": []}

        return Response()


def make_minecraft(tmp_path):
    mc = tmp_path / ".minecraft"
    (mc / "versions" / "fabric-loader-0.16.5-1.21.1").mkdir(parents=True)
    (mc / "versions" / "fabric-loader-0.16.5-1.21.1" / "fabric-loader-0.16.5-1.21.1.json").write_text(
        json.dumps({"id": "fabric-loader-0.16.5-1.21.1", "inheritsFrom": "1.21.1"}))
    (mc / "mods").mkdir()
    (tmp_path / "fabric-pack" / "mods").mkdir(parents=True)
    (tmp_path / "skyclient" / "mods").mkdir(parents=True)
    profiles = {
        "a": {"type": "latest-release", "lastVersionId": "latest-release", "lastUsed": "2026-01-05T00:00:00.000Z"},
        "b": {"type": "custom", "name": "Fabric Pack", "lastVersionId": "fabric-loader-0.16.5-1.21.1",
              "gameDir": str(tmp_path / "fabric-pack"), "lastUsed": "2026-01-04T00:00:00.000Z"},
        "c": {"type": "custom", "name": "SkyClient", "lastVersionId": "1.8.9-forge1.8.9-11.15.1.2318-1.8.9",
              "gameDir": str(tmp_path / "skyclient"), "lastUsed": "2026-01-03T00:00:00.000Z"},
        "d": {"type": "custom", "name": "Vanilla", "lastVersionId": "1.21.4", "gameDir": str(tmp_path / "vanilla"),
              "lastUsed": "2026-01-02T00:00:00.000Z"},
        "e": {"type": "custom", "name": "Old Fabric", "lastVersionId": "fabric-loader-0.15.0-1.20.1",
              "lastUsed": "2026-01-01T00:00:00.000Z"},  # Без gameDir — сама .minecraft
    }
    (mc / official_launcher.PROFILES_FILE).write_text(json.dumps({"profiles": profiles, "version": 3}))
    return str(mc)


def test_installations_become_instances(tmp_path):
    mc = make_minecraft(tmp_path)
    found = {i.name: i for i in launchers.find_official_instances(mc)}
    assert sorted(found) == ["Fabric Pack", "Old Fabric", "SkyClient"]  # «Последний выпуск» и ванильная — нет
    fabric = found["Fabric Pack"]
    assert (fabric.game_version, fabric.loader, fabric.loader_version, fabric.launcher_id) == ("1.21.1", "fabric",
                                                                                                "0.16.5", "b")
    assert fabric.to_profile("release")["launcher_id"] == "b" and fabric.to_profile("release")["manual_mode"]
    sky = found["SkyClient"]
    assert (sky.game_version, sky.loader, sky.loader_version) == ("1.8.9", "forge", "11.15.1.2318")
    assert found["Old Fabric"].game_dir == mc and found["Old Fabric"].game_version == "1.20.1"


def test_old_forge_ids():
    assert extract_loader("1.8.9-forge1.8.9-11.15.1.2318-1.8.9") == "forge"
    assert extract_loader("neoforge-21.1.77") == "neoforge" and extract_loader("1.21.4") is None
    assert extract_loader_version("forge", "1.20.1-forge-47.2.0") == "47.2.0"


def test_install_loader_and_change_installation(tmp_path):
    mc = make_minecraft(tmp_path)
    session = FakeSession()
    vid = official_launcher.install_loader(mc, "fabric", "1.21.4", "0.16.10", session)
    assert vid == "fabric-loader-0.16.10-1.21.4"
    assert session.urls == ["https://meta.fabricmc.net/v2/versions/loader/1.21.4/0.16.10/profile/json"]
    data = json.load(open(os.path.join(mc, "versions", vid, f"{vid}.json")))
    assert data["id"] == vid and os.path.isfile(os.path.join(mc, "versions", vid, f"{vid}.jar"))
    official_launcher.install_loader(mc, "fabric", "1.21.4", "0.16.10", session)
    assert len(session.urls) == 1  # Уже стоит — второй раз не качаем

    target = launcher_pack.LauncherTarget("official", mc, "b", "1.21.1")
    backup = launcher_pack.change_versions(target, "1.21.4", "fabric", "0.16.10", session)
    assert official_launcher.read_profiles(mc)["profiles"]["b"]["lastVersionId"] == vid
    launcher_pack.undo(target, backup)
    assert official_launcher.read_profiles(mc)["profiles"]["b"]["lastVersionId"] == "fabric-loader-0.16.5-1.21.1"


def test_add_installation(tmp_path):
    mc = str(tmp_path / "fresh")  # Лаунчер ещё не запускали — файла нет
    key, _ = official_launcher.add_installation(mc, "Сборка", "fabric-loader-0.16.10-1.21.4", str(tmp_path / "game"))
    profile = official_launcher.read_profiles(mc)["profiles"][key]
    assert profile["name"] == "Сборка" and profile["type"] == "custom"
    assert profile["gameDir"] == str(tmp_path / "game") and profile["lastVersionId"] == "fabric-loader-0.16.10-1.21.4"
    official_launcher.mark_last_used(mc, key)
    assert official_launcher.installations(mc)[0]["id"] == key
