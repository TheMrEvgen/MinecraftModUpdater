"""CurseForge без сети: отпечатки, перевод в формат Modrinth, выбор версии, встраивание в проверку обновлений."""
import os

import curseforge
from enums import ModStatus, UpdateChannel


def cf_file(file_id, name, date, versions=("1.21.1", "Fabric"), release=curseforge.RELEASE, url=True, deps=()):
    return {"id": file_id, "modId": 100, "displayName": name, "fileName": f"{name}.jar", "fileDate": date,
            "releaseType": release, "gameVersions": list(versions), "fileLength": 1000, "isAvailable": True,
            "downloadUrl": f"https://edge.forgecdn.net/files/{file_id}/{name}.jar" if url else None,
            "hashes": [{"algo": curseforge.HASH_SHA1, "value": "a" * 40}],
            "dependencies": [{"modId": m, "relationType": r} for m, r in deps]}


def cf_mod(allow=True, available=True):
    return {"id": 100, "name": "Карта", "slug": "map", "summary": "Мини-карта", "isAvailable": available,
            "allowModDistribution": allow, "links": {"websiteUrl": "https://www.curseforge.com/minecraft/mc-mods/map"},
            "logo": {"thumbnailUrl": "https://img/logo.png"}, "categories": [{"name": "Map and Information"}],
            "authors": [{"name": "автор"}], "latestFilesIndexes": [{"modLoader": 4}, {"modLoader": 6}]}


def test_fingerprint_ignores_whitespace_and_handles_tails():
    assert curseforge.fingerprint(b"a b\nc\td\r") == curseforge.fingerprint(b"abcd")
    values = {curseforge.fingerprint(b"x" * n) for n in range(0, 9)}
    assert len(values) == 9  # Разные длины (и хвосты 0–3 байта) — разные отпечатки
    assert curseforge.fingerprint(b"") == curseforge.fingerprint(b" \n")  # Только пробелы — как пустой файл
    assert all(0 <= v < 2 ** 32 for v in values)


def test_ids():
    assert curseforge.project_id(32274) == "cf:32274" and curseforge.mod_id("cf:32274") == 32274
    assert curseforge.is_curseforge("cf:1") and not curseforge.is_curseforge("AANobbMI") and curseforge.mod_id("AANobbMI") is None


def test_to_version_and_manual_download():
    file = cf_file(5, "map-2.0", "2026-01-02T00:00:00Z", deps=[(7, 3), (8, 2), (9, 5)])
    version = curseforge.to_version(file, cf_mod())
    assert version["project_id"] == "cf:100" and version["version_type"] == "release"
    assert version["files"][0]["url"].endswith("map-2.0.jar") and version["files"][0]["hashes"] == {"sha1": "a" * 40}
    assert [d["dependency_type"] for d in version["dependencies"]] == ["required", "optional", "incompatible"]
    assert version["loaders"] == ["fabric"] and version["game_versions"] == ["1.21.1"] and not version["manual_url"]
    blocked = curseforge.to_version(file, cf_mod(allow=False))  # Автор запретил скачивание программами
    assert blocked["files"][0]["url"] is None and blocked["manual_url"].endswith("/map/files/5")


def test_to_project():
    project = curseforge.to_project(cf_mod())
    assert project["id"] == "cf:100" and project["title"] == "Карта" and project["icon_url"] == "https://img/logo.png"
    assert project["loaders"] == ["fabric", "neoforge"] and project["page_url"].endswith("/map")
    assert project["source"] == "curseforge" and project["categories"] == ["Map and Information"]


def test_latest_file_respects_channel_and_loader():
    files = [cf_file(4, "beta", "2026-04-01", release=curseforge.BETA),
             cf_file(3, "forge-only", "2026-03-01", versions=("1.21.1", "Forge")),
             cf_file(2, "release", "2026-02-01"),
             cf_file(1, "other-version", "2026-05-01", versions=("1.20.1", "Fabric"))]
    files.sort(key=lambda f: f["fileDate"], reverse=True)
    assert curseforge.latest_file(files, "1.21.1", "fabric", "release")["displayName"] == "release"
    assert curseforge.latest_file(files, "1.21.1", "fabric", "beta")["displayName"] == "beta"
    assert curseforge.latest_file(files, "1.21.1", "quilt", "release")["displayName"] == "release"  # Quilt берёт Fabric
    assert curseforge.latest_file(files, "1.21.1", "forge", "release")["displayName"] == "forge-only"
    assert curseforge.latest_file(files, "1.19.2", "fabric", "alpha") is None


def test_html_to_markdown_and_links():
    html = ('<h2><img src="x.png"></h2><p><strong>Карта</strong> &amp; компас</p><ul><li>Раз</li><li>Два</li></ul>'
            '<a href="/linkout?remoteUrl=https%253a%252f%252fexample.com%252fwiki">вики</a><a href="x"><img src="y"></a>')
    text = curseforge.html_to_markdown(html)
    assert "**Карта** & компас" in text and "- Раз\n- Два" in text
    assert "[вики](https://example.com/wiki)" in text and "[]" not in text and not text.startswith("#")


def test_scanner_uses_curseforge_for_unknown_files(tmp_path, monkeypatch):
    import scanner
    jar = tmp_path / "map-1.0.jar"
    jar.write_bytes(b"PK old map")
    gone = tmp_path / "reupload.jar"
    gone.write_bytes(b"PK deleted project")
    locals_ = [{"path": str(jar), "sha1": "s1", "name": "map", "version": "1.0", "size": 10},
               {"path": str(gone), "sha1": "s2", "name": "reupload", "version": "1", "size": 10}]
    installed = cf_file(1, "map-1.0", "2026-01-01")
    deleted = {**cf_mod(available=False), "id": 200}
    monkeypatch.setattr(curseforge, "available", lambda: True)
    monkeypatch.setattr(curseforge, "match_fingerprints", lambda prints: {
        curseforge.fingerprint(b"PKoldmap"): {"file": installed, "mod_id": 100},
        curseforge.fingerprint(b"PKdeletedproject"): {"file": {**installed, "id": 9, "modId": 200}, "mod_id": 200}})
    monkeypatch.setattr(curseforge, "get_mods", lambda ids: {100: cf_mod(allow=False), 200: deleted})
    monkeypatch.setattr(curseforge, "files_for", lambda mod, gv, loader: [cf_file(2, "map-2.0", "2026-02-01"), installed])
    cache = scanner.ModCache(str(tmp_path / "cache.db"))
    try:
        result = scanner._curseforge_entries(locals_, "1.21.1", "fabric", UpdateChannel.RELEASE, cache)
        assert list(result) == [str(jar)]  # Удалённый проект (перезалив) не считается
        entry = result[str(jar)]
        assert entry["project_id"] == "cf:100" and entry["source"] == "curseforge" and entry["name"] == "Карта"
        assert entry["status"] == ModStatus.UPDATE_AVAILABLE and entry["new_version"] == "map-2.0"
        assert entry["download_url"] is None and entry["manual_url"].endswith("/files/2")  # Скачивание только с сайта
        assert cache.get_fingerprints(["s1", "s2"]) == {"s1": curseforge.fingerprint(b"PKoldmap"),
                                                        "s2": curseforge.fingerprint(b"PKdeletedproject")}
    finally:
        cache.close()


def test_without_key_curseforge_is_skipped(tmp_path):
    import scanner
    assert os.environ.get("CURSEFORGE_API_KEY") == "" and not curseforge.available()
    assert scanner._curseforge_entries([{"path": "x", "sha1": "s"}], "1.21.1", "fabric", UpdateChannel.RELEASE, None) == {}


def test_dependencies_satisfied_from_any_catalog(monkeypatch):
    import scanner
    parent = {"name": "Ящики", "slug": "drawers", "installed_info": {"dependencies": [
        {"project_id": "cf:306612", "dependency_type": "required"},
        {"project_id": "cf:500", "dependency_type": "required"},
        {"project_id": "cf:600", "dependency_type": "optional"}]}}
    api = {**cf_mod(), "id": 306612, "name": "Fabric API", "slug": "fabric-api"}
    lib = {**cf_mod(), "id": 500, "name": "Lib Forge Only", "slug": "lib", "latestFilesIndexes": [{"modLoader": 1}]}
    monkeypatch.setattr(curseforge, "get_mods", lambda ids: {306612: api, 500: lib})
    monkeypatch.setattr(curseforge, "files_for", lambda mod, gv, loader: [cf_file(9, "dep-1.0", "2026-01-01")])
    result = {"a.jar": parent}
    # Fabric API уже стоит с Modrinth (тот же адрес), Lib — только для Forge: обе зависимости не нужны
    scanner._attach_curseforge_dependencies(result, {"fabricapi"}, "1.21.1", "fabric", UpdateChannel.RELEASE)
    assert parent.get("dependencies", []) == []
    # Без Fabric API в сборке — требуется, с версией для установки
    scanner._attach_curseforge_dependencies(result, set(), "1.21.1", "fabric", UpdateChannel.RELEASE)
    deps = parent["dependencies"]
    assert [(d["name"], d["status"]) for d in deps] == [("Fabric API", ModStatus.REQUIRED)]
    assert deps[0]["download_url"] and deps[0]["new_version"] == "dep-1.0"


def test_curseforge_pack_import(tmp_path, monkeypatch):
    import json
    import zipfile

    import scanner
    manifest = {"manifestType": "minecraftModpack", "name": "Пак",
                "minecraft": {"version": "1.21.1", "modLoaders": [{"id": "fabric-0.16.5", "primary": True}]},
                "files": [{"projectID": 100, "fileID": 1, "required": True},
                          {"projectID": 300, "fileID": 3, "required": True},   # ресурспак — пропускается
                          {"projectID": 400, "fileID": 4, "required": False}]}  # необязательный — пропускается
    pack = tmp_path / "pack.zip"
    with zipfile.ZipFile(pack, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
    not_pack = tmp_path / "shader.zip"
    with zipfile.ZipFile(not_pack, "w") as z:
        z.writestr("shaders/x.fsh", "x")
    assert scanner.is_curseforge_pack(str(pack)) and not scanner.is_curseforge_pack(str(not_pack))
    loaded = scanner.read_curseforge_pack(str(pack))
    assert scanner.curseforge_pack_game_info(loaded) == ("1.21.1", "fabric")
    monkeypatch.setattr(curseforge, "available", lambda: True)
    monkeypatch.setattr(curseforge, "get_files_by_ids", lambda ids: {1: cf_file(1, "map-1.0", "2026"), 3: cf_file(3, "rp", "2026")})
    monkeypatch.setattr(curseforge, "get_mods", lambda ids: {100: cf_mod(allow=False), 300: {**cf_mod(), "id": 300, "classId": 12}})
    entries = scanner.entries_from_curseforge_pack(loaded)
    assert [e["project_id"] for e in entries] == ["cf:100"]
    assert entries[0]["status"] == ModStatus.READY_TO_INSTALL and entries[0]["download_url"] is None and entries[0]["manual_url"]
