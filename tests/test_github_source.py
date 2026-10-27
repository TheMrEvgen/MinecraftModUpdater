"""GitHub Releases как источник: ссылки, выбор файла выпуска, связь с модом (сама и вручную), кэш и лимит запросов."""
import json
import zipfile

import pytest

import github_source as gh
import scanner
from enums import ModStatus


class Response:
    def __init__(self, status, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data, headers or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class Session:
    def __init__(self, releases):
        self.releases, self.calls = releases, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(headers.get("If-None-Match"))
        if headers.get("If-None-Match") == '"v1"':
            return Response(304)
        return Response(200, self.releases, {"ETag": '"v1"'})


def release(rid, tag, date, assets, name="", prerelease=False):
    return {"id": rid, "tag_name": tag, "name": name, "published_at": date, "prerelease": prerelease, "draft": False,
            "body": f"Changes in {tag}", "assets": [{"id": rid * 10 + i, "name": a, "size": 10,
                                                      "browser_download_url": f"https://github.com/dl/{a}"}
                                                     for i, a in enumerate(assets)]}


RELEASES = [
    release(4, "v2.1.0", "2026-05-01", ["coolmod-fabric-2.1.0+mc1.21.4.jar"], prerelease=False),
    release(3, "v2.0.1", "2026-04-01", ["coolmod-fabric-2.0.1+mc1.21.1.jar", "coolmod-neoforge-2.0.1+mc1.21.1.jar",
                                        "coolmod-fabric-2.0.1+mc1.21.1-sources.jar"]),
    release(2, "v2.0.1-beta", "2026-03-15", ["coolmod-fabric-2.0.1-beta+mc1.21.1.jar"], prerelease=True),
    release(1, "v2.0.0", "2026-03-01", ["coolmod-fabric-2.0.0+mc1.21.1.jar"]),
]


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(gh, "LINKS_FILE", str(tmp_path / "links.json"))
    monkeypatch.setattr(gh, "CACHE_FILE", str(tmp_path / "cache.json"))
    return tmp_path


def make_jar(path, sources="https://github.com/Author/CoolMod", version="2.0.0"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("fabric.mod.json", json.dumps({"id": "coolmod", "name": "Cool Mod", "version": version,
                                                  "description": "Very cool", "contact": {"sources": sources}}))
    return {"path": str(path), "name": "Cool Mod", "version": version, "sha1": None, "size": 1}


def scan(local, session, game_version="1.21.1", channel=("release",)):
    return gh.entries_for([local], game_version, "fabric", list(channel),
                          lambda l: scanner.make_local_entry(l, ModStatus.UP_TO_DATE), scanner.apply_install_info,
                          session=session).get(local["path"])


def test_parse_repo_and_keys():
    assert gh.parse_repo("https://github.com/CaffeineMC/lithium-fabric/issues") == "CaffeineMC/lithium-fabric"
    assert gh.parse_repo("git@github.com:Owner/Repo.git") == "Owner/Repo"
    assert gh.parse_repo("Owner/Repo") == "Owner/Repo"
    assert gh.parse_repo("https://modrinth.com/mod/sodium") is None
    assert gh.link_key("mods/cool-mod-2.0.jar") == gh.link_key("mods/cool-mod-2.1.jar.disabled") == "file:coolmod"


def test_asset_fits():
    fits = gh.asset_fits
    assert fits("coolmod-fabric-2.0.1+mc1.21.1.jar", "", "1.21.1", "fabric")
    assert not fits("coolmod-neoforge-2.0.1+mc1.21.1.jar", "", "1.21.1", "fabric")
    assert fits("coolmod-fabric-2.0.1.jar", "", "1.21.1", "quilt")  # Quilt запускает моды Fabric
    assert not fits("coolmod-fabric-2.1.0+mc1.21.4.jar", "", "1.21.1", "fabric")
    assert not fits("coolmod-fabric-2.0.1+mc1.21.1-sources.jar", "", "1.21.1", "fabric")
    assert fits("modmenu-11.0.5.jar", "v11.0.5 for 1.21.x", "1.21.1", "fabric")
    assert not fits("modmenu-18.0.2.jar", "v18.0.2 for 26.1", "1.21.1", "fabric")
    assert fits("modmenu-11.0.5.jar", "v11.0.5 for 1.21", "1.21.1", "fabric", installed_versions=frozenset({"1.21"}))
    assert fits("tool-3.2.jar", "Release 3.2", "1.21.1", "fabric")  # Версия игры нигде не названа
    assert not fits("tool-3.2.jar", "Release 3.2", "1.21.1", "fabric", installed_name="tool-3.1-1.21.1.jar")


def test_auto_link_by_metadata_and_update(files):
    local = make_jar(files / "coolmod-fabric-2.0.0+mc1.21.1.jar")
    entry = scan(local, Session(RELEASES))
    assert entry["project_id"] == "gh:Author/CoolMod" and entry["source"] == "github"
    assert entry["installed_info"]["version_number"] == "v2.0.0"
    assert entry["status"] == ModStatus.UPDATE_AVAILABLE and entry["new_version"] == "v2.0.1"  # Не бета и не 1.21.4
    assert entry["new_filename"] == "coolmod-fabric-2.0.1+mc1.21.1.jar" and entry["description"] == "Very cool"
    beta = scan(local, Session(RELEASES), channel=("release", "beta"))
    assert beta["new_version"] == "v2.0.1"  # Релиз 2.0.1 новее беты


def test_unconfirmed_metadata_link_is_ignored(files):
    local = make_jar(files / "renamed-mod.jar", version="9.9.9")  # Такого файла и версии в выпусках нет
    assert scan(local, Session(RELEASES)) is None


def test_manual_link_and_unlink(files):
    local = make_jar(files / "custom-build.jar", sources="", version="9.9.9")
    assert scan(local, Session(RELEASES)) is None
    key = gh.link_key(local["path"], "coolmod")
    gh.set_link(key, "Author/CoolMod")
    entry = scan(local, Session(RELEASES))
    assert entry["new_version"] == "v2.0.1" and "installed_info" not in entry
    gh.set_link(key, "")  # «Отвязать»: и по ссылке в метаданных больше не искать
    assert scan(make_jar(files / "custom-build.jar", version="9.9.9"), Session(RELEASES)) is None


def test_cache_etag_and_rate_limit(files, monkeypatch):
    session = Session(RELEASES)
    cache = gh._Cache(gh.CACHE_FILE)
    assert len(gh.releases("Author/CoolMod", cache, session)) == 4
    gh.releases("Author/CoolMod", cache, session)
    assert session.calls == [None]  # Второй раз — из кэша без запроса
    monkeypatch.setattr(gh, "FRESH_SECONDS", -1)
    gh.releases("Author/CoolMod", cache, session)
    assert session.calls == [None, '"v1"']  # Устарело — запрос с ETag, ответ 304

    class Limited:
        def get(self, url, headers=None, timeout=None):
            return Response(403, None, {"X-RateLimit-Remaining": "0"})

    assert len(gh.releases("Author/CoolMod", cache, Limited())) == 4  # Лимит — отдаём запомненное
    with pytest.raises(gh.RateLimited):
        gh.releases("Other/Repo", cache, Limited())
