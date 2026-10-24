"""Правка mmc-pack.json сборки Prism/MultiMC: загрузчик, версия игры, копия и откат (без сети)."""
import json

import pytest

import launcher_pack

PACK = {"formatVersion": 1, "components": [
    {"uid": "org.lwjgl3", "version": "3.3.3", "cachedVersion": "3.3.3", "dependencyOnly": True},
    {"uid": "net.minecraft", "version": "1.21.1", "cachedVersion": "1.21.1", "important": True,
     "cachedRequires": [{"uid": "org.lwjgl3", "suggests": "3.3.3"}]},
    {"uid": "net.fabricmc.intermediary", "version": "1.21.1", "cachedVersion": "1.21.1", "dependencyOnly": True},
    {"uid": "net.fabricmc.fabric-loader", "version": "0.16.5", "cachedVersion": "0.16.5",
     "cachedRequires": [{"uid": "net.fabricmc.intermediary"}]}]}


class FakeSession:
    def __init__(self, requires):
        self.requires, self.urls = requires, []

    def get(self, url, timeout=None):
        self.urls.append(url)
        session = self

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"requires": session.requires}

        return Response()


@pytest.fixture
def instance(tmp_path):
    (tmp_path / "minecraft" / "mods").mkdir(parents=True)
    (tmp_path / "mmc-pack.json").write_text(json.dumps(PACK), encoding="utf-8")
    return tmp_path


def components(instance):
    return {c["uid"]: c for c in json.loads((instance / "mmc-pack.json").read_text(encoding="utf-8"))["components"]}


def test_find_and_read(instance, tmp_path_factory):
    path = launcher_pack.pack_file(str(instance / "minecraft"))
    assert path == str(instance / "mmc-pack.json")
    assert launcher_pack.versions(path) == ("1.21.1", "fabric", "0.16.5")
    assert launcher_pack.pack_file(str(tmp_path_factory.mktemp("plain"))) is None and launcher_pack.pack_file("") is None


def test_loader_update_and_undo(instance):
    path = launcher_pack.pack_file(str(instance / "minecraft"))
    backup = launcher_pack.set_loader_version(path, "fabric", "0.17.2")
    loader = components(instance)["net.fabricmc.fabric-loader"]
    assert loader["version"] == loader["cachedVersion"] == "0.17.2" and "cachedRequires" not in loader
    launcher_pack.restore(path, backup)
    assert components(instance)["net.fabricmc.fabric-loader"]["version"] == "0.16.5"
    with pytest.raises(ValueError):
        launcher_pack.set_loader_version(path, "neoforge", "21.1.1")


def test_game_version_change_follows_meta(instance):
    path = launcher_pack.pack_file(str(instance / "minecraft"))
    session = FakeSession([{"uid": "org.lwjgl3", "suggests": "3.3.6"}])
    launcher_pack.set_game_version(path, "1.21.11", "fabric", "0.18.0", session)
    result = components(instance)
    assert session.urls == ["https://meta.prismlauncher.org/v1/net.minecraft/1.21.11.json"]
    assert result["net.minecraft"]["version"] == "1.21.11" and result["net.minecraft"]["cachedRequires"][0]["suggests"] == "3.3.6"
    assert result["org.lwjgl3"]["version"] == "3.3.6"
    assert result["net.fabricmc.intermediary"]["version"] == "1.21.11"
    assert result["net.fabricmc.fabric-loader"]["version"] == "0.18.0"
    assert len(list(instance.glob("mmc-pack.json.modupdater-*.bak"))) == 1
