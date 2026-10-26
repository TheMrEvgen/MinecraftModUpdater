"""Установка готовой сборки: чтение .mrpack, overrides, защита путей, экземпляр Prism, проверка хэшей (без сети)."""
import hashlib
import json
import os
import zipfile

import modpack_install


def make_mrpack(path, files, overrides, version="1.0"):
    index = {"formatVersion": 1, "game": "minecraft", "versionId": version, "name": "Пак",
             "dependencies": {"minecraft": "1.21.1", "fabric-loader": "0.16.5"}, "files": files}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("modrinth.index.json", json.dumps(index))
        for name, data in overrides.items():
            z.writestr(name, data)


class FakeSession:
    def __init__(self, contents):
        self.contents = contents

    def get(self, url, timeout=None, stream=False, headers=None):
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


def _file(name, data):
    return {"path": f"mods/{name}", "downloads": [f"https://cdn.modrinth.com/{name}"],
            "hashes": {"sha1": hashlib.sha1(data).hexdigest()}, "fileSize": len(data)}


def test_update_keeps_user_mods_and_changed_configs(tmp_path, monkeypatch):
    game = str(tmp_path / "game")
    v1 = tmp_path / "v1.mrpack"
    make_mrpack(v1, [_file("a-1.jar", b"a1"), _file("b.jar", b"b"), _file("gone.jar", b"g"), _file("off.jar", b"o1")],
                {"overrides/config/a.toml": "a=1", "overrides/config/b.toml": "b=1"})
    session = FakeSession({"https://cdn.modrinth.com/a-1.jar": b"a1", "https://cdn.modrinth.com/b.jar": b"b",
                           "https://cdn.modrinth.com/gone.jar": b"g", "https://cdn.modrinth.com/off.jar": b"o1",
                           "https://cdn.modrinth.com/a-2.jar": b"a2", "https://cdn.modrinth.com/new.jar": b"n"})
    monkeypatch.setattr("mod_updater_logic.SESSION", session)
    plan1 = modpack_install.load_plan(str(v1))
    assert modpack_install.install(plan1, modpack_install.Target(game), session=session) == []
    modpack_install.save_state(plan1, "proj", game, "Пак")
    # Пользователь: свой мод, правка настройки, выключенный мод сборки (off.jar поменяется в новой версии)
    open(os.path.join(game, "mods", "mine.jar"), "wb").write(b"mine")
    open(os.path.join(game, "config", "b.toml"), "w").write("b=my")
    os.rename(os.path.join(game, "mods", "off.jar"), os.path.join(game, "mods", "off.jar.disabled"))

    v2 = tmp_path / "v2.mrpack"
    files2 = [_file("a-2.jar", b"a2"), _file("b.jar", b"b"), _file("new.jar", b"n"), _file("off.jar", b"o2")]
    session.contents["https://cdn.modrinth.com/off.jar"] = b"o2"
    make_mrpack(v2, files2, {"overrides/config/a.toml": "a=2", "overrides/config/b.toml": "b=2",
                             "overrides/config/c.toml": "c=2"}, version="2.0")
    plan2 = modpack_install.load_plan(str(v2))
    state = modpack_install.load_state(game)
    update = modpack_install.plan_update(state, plan2, game)
    assert sorted(f.path for f in update.download) == ["mods/a-2.jar", "mods/new.jar", "mods/off.jar"]
    assert update.remove == ["mods/a-1.jar", "mods/gone.jar"]
    assert update.overrides == ["config/a.toml", "config/c.toml"] and update.kept == ["config/b.toml"]
    assert update.user_mods == ["mine.jar"]

    backup = modpack_install.apply_update(update, plan2, game, session=session)
    mods = sorted(os.listdir(os.path.join(game, "mods")))
    assert mods == ["a-2.jar", "b.jar", "mine.jar", "new.jar", "off.jar.disabled"]
    assert open(os.path.join(game, "mods", "off.jar.disabled"), "rb").read() == b"o2"
    assert open(os.path.join(game, "config", "a.toml")).read() == "a=2"
    assert open(os.path.join(game, "config", "b.toml")).read() == "b=my"
    assert modpack_install.load_state(game)["version"] == "2.0"

    modpack_install.undo_update(backup, game)
    assert sorted(os.listdir(os.path.join(game, "mods"))) == ["a-1.jar", "b.jar", "gone.jar", "mine.jar",
                                                               "off.jar.disabled"]
    assert open(os.path.join(game, "mods", "off.jar.disabled"), "rb").read() == b"o1"
    assert open(os.path.join(game, "config", "a.toml")).read() == "a=1"
    assert not os.path.exists(os.path.join(game, "config", "c.toml"))
    assert modpack_install.load_state(game)["version"] == "1.0" and not os.path.exists(backup)


def test_update_failure_leaves_pack_untouched(tmp_path, monkeypatch):
    game = str(tmp_path / "game")
    v1 = tmp_path / "v1.mrpack"
    make_mrpack(v1, [_file("a-1.jar", b"a1")], {})
    session = FakeSession({"https://cdn.modrinth.com/a-1.jar": b"a1", "https://cdn.modrinth.com/a-2.jar": b"broken"})
    monkeypatch.setattr("mod_updater_logic.SESSION", session)
    plan1 = modpack_install.load_plan(str(v1))
    modpack_install.install(plan1, modpack_install.Target(game), session=session)
    modpack_install.save_state(plan1, "proj", game)
    v2 = tmp_path / "v2.mrpack"
    make_mrpack(v2, [_file("a-2.jar", b"a2")], {}, version="2.0")
    plan2 = modpack_install.load_plan(str(v2))
    update = modpack_install.plan_update(modpack_install.load_state(game), plan2, game)
    try:
        modpack_install.apply_update(update, plan2, game, session=session)
        assert False, "контрольная сумма должна была не совпасть"
    except ValueError:
        pass
    assert os.listdir(os.path.join(game, "mods")) == ["a-1.jar"]
    assert modpack_install.load_state(game)["version"] == "1.0"


def test_download_progress_shows_bytes(tmp_path):
    data = b"x" * 3000
    session = FakeSession({"https://cdn.modrinth.com/big.jar": data})
    item = modpack_install.PackFile("mods/big.jar", "https://cdn.modrinth.com/big.jar",
                                    {"sha1": hashlib.sha1(data).hexdigest()}, len(data), name="Big")
    reports = []
    errors = modpack_install.download_files([(item, str(tmp_path / "big.part"))],
                                            lambda d, t, s: reports.append((d, t, s)), lambda: False, session)
    assert errors == {} and (tmp_path / "big.part").read_bytes() == data
    done, total, text = reports[-1]
    from view_model import format_size
    assert (done, total) == (1, 1) and text.startswith("Big · ") and text.count(format_size(len(data))) == 2


class FlakySession:
    """Первый раз рвёт связь на середине файла; потом отдаёт остаток по заголовку Range (206)."""

    def __init__(self, data):
        self.data, self.ranges = data, []

    def get(self, url, timeout=None, stream=False, headers=None):
        import requests
        start = int((headers or {}).get("Range", "bytes=0-")[6:-1] or 0)
        self.ranges.append(start)
        session = self

        class Response:
            status_code = 206 if start else 200
            headers = {"Content-Length": str(len(session.data) - start)}

            def raise_for_status(self):
                pass

            def iter_content(self, size):
                body = session.data[start:]
                if len(session.ranges) == 1:  # Первая попытка: половина и обрыв
                    yield body[:len(body) // 2]
                    raise requests.ConnectionError("обрыв")
                yield body

        return Response()


def test_download_resumes_after_connection_drop(tmp_path, monkeypatch):
    import downloads
    monkeypatch.setattr("time.sleep", lambda s: None)
    data = bytes(range(256)) * 40
    session = FlakySession(data)
    item = modpack_install.PackFile("mods/a.jar", "https://cdn.modrinth.com/a.jar", {"sha1": hashlib.sha1(data).hexdigest()},
                                    len(data), name="A")
    job = downloads.DownloadJob("t")
    errors = modpack_install.download_files([(item, str(tmp_path / "a.part"))], lambda *a: None, lambda: False, session,
                                            job=job)
    assert errors == {} and (tmp_path / "a.part").read_bytes() == data
    assert session.ranges == [0, len(data) // 2]  # Второй запрос — с места обрыва
    assert job.files["mods/a.jar"].state == downloads.DONE
