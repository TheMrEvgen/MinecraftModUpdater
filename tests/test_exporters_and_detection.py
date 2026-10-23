import json

import pytest

from exporters import (
    build_mrpack_index, build_share_data, decode_share_string, encode_share_string, mrpack_env, parse_share_data,
)
from game_detection import detect_game_versions, extract_game_version, extract_loader, extract_loader_version


def _installed(pid, version="1.0", client="required", server="optional", deps=None):
    return {
        "project_id": pid, "name": pid, "current_version": version, "filepath": f"/mods/{pid}.jar",
        "client_side": client, "server_side": server, "dependencies": deps or [],
        "installed_info": {"files": [{"filename": f"{pid}.jar", "primary": True, "url": f"https://cdn.modrinth.com/data/x/versions/y/{pid}.jar",
                                      "size": 10, "hashes": {"sha1": "s1", "sha512": "s512"}}]},
    }


def test_share_roundtrip_includes_installed_dependencies():
    dep = _installed("fabric-api", "0.90")
    dep["installed"] = True
    data = build_share_data([_installed("sodium", deps=[dep])], "1.21.1", "fabric", strict=True)
    assert data["mods"] == {"sodium": "1.0", "fabric-api": "0.90"}
    decoded = decode_share_string(encode_share_string(data))
    request = parse_share_data(decoded)
    assert request.strict and request.game_version == "1.21.1" and set(request.project_ids) == {"sodium", "fabric-api"}


def test_parse_share_data_v1_and_errors():
    assert parse_share_data({"v": 1, "mc": "1.20.1", "l": "forge", "p": ["a", "b"]}).project_ids == ["a", "b"]
    with pytest.raises(ValueError):
        parse_share_data({"v": 9, "mc": "1", "l": "x"})
    with pytest.raises(ValueError):
        parse_share_data([])
    with pytest.raises(ValueError):
        decode_share_string("definitely-not-valid")


def test_mrpack_env_mapping():
    assert mrpack_env({"client_side": "required", "server_side": "unsupported"}) == {"client": "required", "server": "unsupported"}
    assert mrpack_env({"client_side": "unknown", "server_side": None}) == {"client": "required", "server": "required"}


def test_build_mrpack_index():
    index = build_mrpack_index([_installed("sodium", client="required", server="unsupported")], "1.21.1", "fabric", "0.16.5")
    assert index["dependencies"] == {"minecraft": "1.21.1", "fabric-loader": "0.16.5"}
    assert index["files"][0]["env"] == {"client": "required", "server": "unsupported"}
    assert index["files"][0]["path"] == "mods/sodium.jar"


class TestGameDetection:
    def test_extract_game_version(self):
        assert extract_game_version({"inheritsFrom": "1.20.1"}) == "1.20.1"
        assert extract_game_version({"id": "fabric-loader-0.15.11-1.20.1"}) == "1.20.1"
        assert extract_game_version({"libraries": [{"name": "net.minecraft:client:1.21.4"}]}) == "1.21.4"

    def test_extract_loader(self):
        assert extract_loader("1.21.1-neoforge-21.1.5") == "neoforge"
        assert extract_loader("1.20.1-forge-47.2.0") == "forge"
        assert extract_loader("fabric-loader-0.16.5-1.21.1") == "fabric"
        assert extract_loader("1.21.1") is None

    def test_extract_loader_version(self):
        assert extract_loader_version("fabric", "fabric-loader-0.16.5-1.21.1") == "0.16.5"
        assert extract_loader_version("neoforge", "neoforge-21.1.5") == "21.1.5"
        assert extract_loader_version("forge", "") is None

    def test_detect_from_folder(self, tmp_path):
        for vid, data in (("fabric-loader-0.16.5-1.21.1", {"inheritsFrom": "1.21.1"}),
                          ("1.21.1", {})):
            folder = tmp_path / "versions" / vid
            folder.mkdir(parents=True)
            (folder / f"{vid}.json").write_text(json.dumps({"id": vid, **data}), encoding="utf-8")
        result = detect_game_versions(str(tmp_path))
        assert result.versions == {("1.21.1", "fabric")}
        assert result.best_guess == ("1.21.1", "fabric")


def test_mrpack_skips_files_outside_allowed_hosts():
    from exporters import build_mrpack_index

    def entry(name, url):
        return {"name": name, "project_id": name, "filepath": f"/mods/{name}.jar",
                "installed_info": {"files": [{"url": url, "filename": f"{name}.jar", "primary": True,
                                              "hashes": {"sha1": "a" * 40, "sha512": "b" * 128}, "size": 1}]}}

    mods = [entry("sodium", "https://cdn.modrinth.com/data/AANobbMI/versions/x/sodium.jar"),
            entry("journeymap", "https://edge.forgecdn.net/files/8923/666/journeymap.jar"),
            entry("blocked", None)]
    skipped = []
    index = build_mrpack_index(mods, "1.21.1", "fabric", "0.16.0", skipped=skipped)
    assert [f["path"] for f in index["files"]] == ["mods/sodium.jar"]
    assert skipped == ["journeymap", "blocked"]
