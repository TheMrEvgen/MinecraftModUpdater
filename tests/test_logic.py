import json
import zipfile

import pytest

from enums import ModStatus, UpdateChannel
from mod_updater_logic import (
    is_newer_version, neoforge_version_prefix, pick_neoforge_version, safe_filename, select_primary_file,
    _find_best_game_version,
)


class TestNeoForge:
    def test_prefix_has_trailing_dot(self):
        assert neoforge_version_prefix("1.21.1") == "21.1."
        assert neoforge_version_prefix("1.21") == "21.0."
        assert neoforge_version_prefix("1.20.4") == "20.4."

    def test_invalid_version_does_not_crash(self):
        assert neoforge_version_prefix("1") is None
        assert neoforge_version_prefix("abc") is None
        assert pick_neoforge_version(["21.1.5"], "1") is None

    def test_1_21_1_does_not_match_1_21_10(self):
        versions = ["21.1.200", "21.1.210", "21.10.5", "21.11.3-beta"]
        assert pick_neoforge_version(versions, "1.21.1") == "21.1.210"

    def test_prefers_stable(self):
        assert pick_neoforge_version(["21.4.10", "21.4.20-beta"], "1.21.4") == "21.4.10"

    def test_falls_back_to_prerelease(self):
        assert pick_neoforge_version(["21.5.1-beta", "21.5.2-beta"], "1.21.5") == "21.5.2-beta"


def test_find_best_game_version_skips_snapshots():
    api = [{"version": "1.21.4"}, {"version": "24w14a"}, {"version": "1.21.5"}, {"version": "1.20.1"}]
    assert _find_best_game_version(api, "1.21.4") == "1.21.4"
    assert _find_best_game_version(api, "1.21.6") == "1.21.5"


class TestIsNewerVersion:
    def test_same_id_is_not_newer(self):
        assert not is_newer_version({"id": "a", "version_number": "1"}, {"id": "a", "version_number": "1"})

    def test_older_release_is_not_an_update_for_newer_beta(self):
        latest_release = {"id": "r", "version_number": "1.0", "date_published": "2024-01-01T00:00:00Z"}
        installed_beta = {"id": "b", "version_number": "1.1-beta", "date_published": "2024-03-01T00:00:00Z"}
        assert not is_newer_version(latest_release, installed_beta)

    def test_newer_by_date(self):
        old = {"id": "o", "version_number": "1.0", "date_published": "2024-01-01T00:00:00Z"}
        new = {"id": "n", "version_number": "1.1", "date_published": "2024-02-01T00:00:00Z"}
        assert is_newer_version(new, old)

    def test_none(self):
        assert not is_newer_version(None, {"id": "x"})
        assert is_newer_version({"id": "x"}, None)


class TestSafeFilename:
    def test_plain(self):
        assert safe_filename("sodium-0.5.jar") == "sodium-0.5.jar"

    @pytest.mark.parametrize("name", ["../evil.jar", "..\\evil.jar", "sub/dir/evil.jar"])
    def test_strips_directories(self, name):
        assert safe_filename(name) == "evil.jar"

    @pytest.mark.parametrize("name", ["", "..", ".", ".hidden"])
    def test_rejects_invalid(self, name):
        with pytest.raises(ValueError):
            safe_filename(name)


def test_select_primary_file_fallback():
    assert select_primary_file({"files": [{"filename": "a"}, {"filename": "b", "primary": True}]})["filename"] == "b"
    assert select_primary_file({"files": [{"filename": "a"}]})["filename"] == "a"
    assert select_primary_file({"files": []}) is None
    assert select_primary_file(None) is None


def test_update_channel_types():
    assert UpdateChannel.RELEASE.version_types == ["release"]
    assert "alpha" not in UpdateChannel.BETA.version_types
    assert UpdateChannel.from_value("garbage") == UpdateChannel.RELEASE


def test_mod_status_is_string():
    assert ModStatus.UPDATE_AVAILABLE == "Доступно обновление"
    assert str(ModStatus.UPDATED) == "Обновлено!"
    assert json.dumps({"s": ModStatus.UP_TO_DATE}) == json.dumps({"s": "Актуально"})
    assert ModStatus.HASH_ERROR.is_error and not ModStatus.UPDATED.is_error


def test_read_mod_metadata_fabric(tmp_path):
    from scanner import read_mod_metadata
    jar = tmp_path / "mod.jar"
    with zipfile.ZipFile(jar, "w") as z:
        z.writestr("fabric.mod.json", json.dumps({"name": "Sodium", "version": "0.5.8",
                                                  "custom": {"modrinth": "AANobbMI"}}))
    assert read_mod_metadata(str(jar)) == ("Sodium", "0.5.8", "AANobbMI")


def test_read_mod_metadata_forge_manifest_fallback(tmp_path):
    from scanner import read_mod_metadata
    jar = tmp_path / "forge.jar"
    with zipfile.ZipFile(jar, "w") as z:
        z.writestr("META-INF/mods.toml", '[[mods]]\nmodId="x"\ndisplayName="Forge Mod"\nversion="${file.jarVersion}"\n')
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\nImplementation-Version: 2.3.4\n")
    assert read_mod_metadata(str(jar)) == ("Forge Mod", "2.3.4", None)


def test_read_mod_metadata_broken_jar(tmp_path):
    from scanner import read_mod_metadata
    jar = tmp_path / "broken-1.0.jar"
    jar.write_bytes(b"not a zip")
    assert read_mod_metadata(str(jar)) == ("broken-1.0", "", None)
