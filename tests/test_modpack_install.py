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


def _zip_bytes(entries):
    import io
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buffer.getvalue()


def test_curseforge_pack_downloads_maps(tmp_path, monkeypatch):
    """Карта, подключённая к сборке CurseForge отдельным проектом (класс Worlds), распаковывается в saves."""
    import curseforge
    world = _zip_bytes({"Остров/level.dat": b"lvl", "Остров/region/r.0.0.mca": b"r"})
    flat = _zip_bytes({"level.dat": b"flat", "region/r.0.0.mca": b"f"})
    jar = b"jar"
    cf_files = {
        1: {"id": 1, "modId": 10, "fileName": "mod.jar", "downloadUrl": "https://edge/mod.jar", "fileLength": 3,
            "hashes": [{"algo": 1, "value": hashlib.sha1(jar).hexdigest()}]},
        2: {"id": 2, "modId": 20, "fileName": "island.zip", "downloadUrl": "https://edge/island.zip", "hashes": []},
        3: {"id": 3, "modId": 30, "fileName": "Плоский мир.zip", "downloadUrl": "https://edge/flat.zip", "hashes": []},
        4: {"id": 4, "modId": 40, "fileName": "dp.zip", "downloadUrl": "https://edge/dp.zip",
            "hashes": [{"algo": 1, "value": hashlib.sha1(b"datapack").hexdigest()}]},
    }
    cf_mods = {10: {"id": 10, "classId": 6, "name": "Мод"}, 20: {"id": 20, "classId": 17, "name": "Остров"},
               30: {"id": 30, "classId": 17, "name": "Плоский"}, 40: {"id": 40, "classId": 6945, "name": "Датапак"}}
    monkeypatch.setattr(curseforge, "available", lambda: True)
    monkeypatch.setattr(curseforge, "get_files_by_ids", lambda ids: {i: cf_files[i] for i in ids})
    monkeypatch.setattr(curseforge, "get_mods", lambda ids: {i: cf_mods[i] for i in ids})
    manifest = {"minecraft": {"version": "1.21.1", "modLoaders": [{"id": "fabric-0.16.5", "primary": True}]},
                "manifestType": "minecraftModpack", "name": "CF", "version": "1", "overrides": "overrides",
                "files": [{"projectID": p, "fileID": f, "required": True} for p, f in ((10, 1), (20, 2), (30, 3), (40, 4))]}
    archive = tmp_path / "cf.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
    plan = modpack_install.load_plan(str(archive))
    assert [(f.path, f.world, f.datapack) for f in plan.files] == [
        ("mods/mod.jar", False, False), ("saves/island.zip", True, False), ("saves/Плоский мир.zip", True, False),
        ("datapacks/dp.zip", False, True)]
    assert plan.kind == "curseforge"
    session = FakeSession({"https://edge/mod.jar": jar, "https://edge/island.zip": world, "https://edge/flat.zip": flat,
                           "https://edge/dp.zip": b"datapack"})
    game = tmp_path / "game"
    assert modpack_install.install(plan, modpack_install.Target(str(game)), session=session) == []
    assert (game / "mods" / "mod.jar").read_bytes() == jar
    assert (game / "saves" / "Остров" / "level.dat").read_bytes() == b"lvl"
    assert (game / "saves" / "Плоский мир" / "region" / "r.0.0.mca").read_bytes() == b"f"
    assert sorted(os.listdir(game / "saves")) == ["Остров", "Плоский мир"]  # Архивы карт удалены
    # Датапак: общая папка datapacks и копия в каждом мире сборки
    assert (game / "datapacks" / "dp.zip").read_bytes() == b"datapack"
    for world in ("Остров", "Плоский мир"):
        assert (game / "saves" / world / "datapacks" / "dp.zip").read_bytes() == b"datapack"

    # Обновление: уже поставленную карту не трогаем (в ней играют), новую — добавляем
    modpack_install.save_state(plan, "cf:1", str(game))
    (game / "saves" / "Остров" / "level.dat").write_bytes(b"played")
    plan.files.append(modpack_install.PackFile("saves/new.zip", "https://edge/new.zip", world=True))
    session.contents["https://edge/new.zip"] = _zip_bytes({"Новая/level.dat": b"n"})
    update = modpack_install.plan_update(modpack_install.load_state(str(game)), plan, str(game))
    assert [f.path for f in update.download] == ["saves/new.zip"] and update.remove == []
    backup = modpack_install.apply_update(update, plan, str(game), session=session)
    assert (game / "saves" / "Новая" / "level.dat").read_bytes() == b"n"
    assert (game / "saves" / "Остров" / "level.dat").read_bytes() == b"played"
    modpack_install.undo_update(backup, str(game))
    assert sorted(os.listdir(game / "saves")) == ["Остров", "Плоский мир"]


def test_unpack_world_never_overwrites_or_escapes(tmp_path):
    saves = tmp_path / "saves"
    (saves / "Мир").mkdir(parents=True)
    (saves / "Мир" / "level.dat").write_bytes(b"mine")
    archive = tmp_path / "w.zip"
    archive.write_bytes(_zip_bytes({"Мир/level.dat": b"theirs", "../evil.txt": b"x", "Другой/level.dat": b"d"}))
    created = modpack_install.unpack_world(str(archive), str(saves))
    assert [os.path.basename(p) for p in created] == ["Другой"]
    assert (saves / "Мир" / "level.dat").read_bytes() == b"mine" and not (tmp_path / "evil.txt").exists()


def test_folder_target_directly_into_chosen_folder(tmp_path):
    chosen = tmp_path / "MyLauncher" / "game"
    target = modpack_install.folder_target(str(chosen), "Пак", own_folder=False)
    assert target.game_dir == str(chosen) and target.fresh  # Папки ещё нет — её можно убрать при отмене
    (chosen / "mods").mkdir(parents=True)
    (chosen / "mods" / "old.jar").write_bytes(b"x")
    target = modpack_install.folder_target(str(chosen), "Пак", own_folder=False)
    assert not target.fresh and modpack_install.folder_has_mods(str(chosen))
    assert modpack_install.folder_target(str(tmp_path), "Пак").game_dir == str(tmp_path / "Пак")

class MetaSession:
    """Справочник Prism: свои сведения для каждого компонента; для одного — ошибка сети."""

    def __init__(self, broken=()):
        self.broken = set(broken)

    def get(self, url, timeout=None, stream=False, headers=None):
        uid = url.rsplit("/", 2)[-2]
        meta = {"org.lwjgl3": {"name": "LWJGL 3", "volatile": True},
                "net.minecraft": {"name": "Minecraft", "requires": [{"uid": "org.lwjgl3", "suggests": "3.3.3"}]},
                "net.neoforged": {"name": "NeoForge", "requires": [{"uid": "net.minecraft", "equals": "1.21.1"}]}}
        broken = uid in self.broken

        class Response:
            def raise_for_status(self):
                if broken:
                    raise OSError("нет сети")

            def json(self):
                return meta.get(uid, {"name": uid})

        return Response()


def test_prism_instance_appears_complete(tmp_path):
    """Сборка Prism появляется в instances сразу целиком: полный instance.cfg и сведения о компонентах."""
    root = tmp_path / "instances"
    target = modpack_install.prism_target(str(root), "FTB: Stone/Block")
    target.name = "FTB: Stone/Block"
    plan = modpack_install.versions_plan("FTB", "1.21.1", "neoforge", "21.1.262")
    modpack_install.prepare_target(target, plan, session=MetaSession())
    assert sorted(os.listdir(root)) == ["FTB_ Stone_Block"]  # Временной папки не осталось
    cfg = open(os.path.join(target.instance_dir, "instance.cfg"), encoding="utf-8").read()
    assert "name=FTB: Stone/Block\n" in cfg and "InstanceType=OneSix" in cfg and "ManagedPack=false" in cfg
    assert "OverrideJavaLocation=false" in cfg and "uuid=" in cfg and cfg.count("\n") > 40
    pack = json.load(open(os.path.join(target.instance_dir, "mmc-pack.json"), encoding="utf-8"))
    by_uid = {c["uid"]: c for c in pack["components"]}
    assert list(by_uid) == ["org.lwjgl3", "net.minecraft", "net.neoforged"]
    assert by_uid["org.lwjgl3"]["cachedVolatile"] and by_uid["org.lwjgl3"]["dependencyOnly"]
    assert by_uid["net.minecraft"]["cachedRequires"] == [{"uid": "org.lwjgl3", "suggests": "3.3.3"}]
    assert by_uid["net.neoforged"]["cachedName"] == "NeoForge" and by_uid["net.neoforged"]["version"] == "21.1.262"
    assert os.path.isdir(target.game_dir)
    from launchers import find_prism_instances  # Программа сама находит такую сборку как сборку Prism
    os.makedirs(tmp_path / "x")
    os.rename(root, tmp_path / "x" / "instances")
    found = find_prism_instances(str(tmp_path / "x"))
    assert [(i.name, i.game_version, i.loader, i.loader_version) for i in found] == [
        ("FTB: Stone/Block", "1.21.1", "neoforge", "21.1.262")]


def test_prism_instance_without_loader_meta(tmp_path):
    """Справочник не ответил про загрузчик — сборка всё равно создаётся, Prism допишет сведения сам."""
    target = modpack_install.prism_target(str(tmp_path), "Пак")
    plan = modpack_install.versions_plan("Пак", "1.21.1", "neoforge", "21.1.262")
    modpack_install.prepare_target(target, plan, session=MetaSession(broken={"net.neoforged"}))
    pack = json.load(open(os.path.join(target.instance_dir, "mmc-pack.json"), encoding="utf-8"))
    assert pack["components"][-1] == {"uid": "net.neoforged", "version": "21.1.262"}
    assert os.listdir(tmp_path) == ["Пак"]


def test_repair_downloads_missing_and_broken(tmp_path, monkeypatch):
    """Проверка сборки: недостающее и испорченное качается заново, выключенное остаётся выключенным."""
    game = str(tmp_path / "game")
    files = [_file("a.jar", b"a"), _file("b.jar", b"b"), _file("c.jar", b"c"), _file("d.jar", b"d")]
    archive = tmp_path / "v1.mrpack"
    make_mrpack(archive, files, {"overrides/config/x.toml": "x=1", "overrides/config/mine.toml": "m=1"})
    session = FakeSession({f"https://cdn.modrinth.com/{n}.jar": n.encode() for n in "abcd"})
    monkeypatch.setattr("mod_updater_logic.SESSION", session)
    plan = modpack_install.load_plan(str(archive))
    modpack_install.install(plan, modpack_install.Target(game), session=session)
    modpack_install.save_state(plan, "proj", game)
    mods = os.path.join(game, "mods")
    os.remove(os.path.join(mods, "a.jar"))                                     # Пропал
    open(os.path.join(mods, "b.jar"), "wb").write(b"oops")                     # Оборвалась загрузка
    os.rename(os.path.join(mods, "c.jar"), os.path.join(mods, "c.jar.disabled"))
    open(os.path.join(mods, "c.jar.disabled"), "wb").write(b"bad")             # Выключенный и испорченный
    os.remove(os.path.join(game, "config", "x.toml"))                          # Пропала настройка
    open(os.path.join(game, "config", "mine.toml"), "w").write("m=my")         # Изменённая — не трогаем
    open(os.path.join(mods, "user.jar"), "wb").write(b"u")                     # Свой мод — не трогаем

    state = modpack_install.load_state(game)
    assert state["archive"] == os.path.abspath(str(archive))
    again = modpack_install.installed_plan(state, str(tmp_path / "dl"))
    repair = modpack_install.plan_repair(again, game)
    assert [f.path for f in repair.missing] == ["mods/a.jar"]
    assert [f.path for f in repair.broken] == ["mods/b.jar", "mods/c.jar"]
    assert repair.overrides == ["config/x.toml"] and repair.checked == 4 and not repair.empty
    assert modpack_install.apply_repair(repair, again, game, session=session) == []
    assert sorted(os.listdir(mods)) == ["ModUpdater_Backups", "a.jar", "b.jar", "c.jar.disabled", "d.jar", "user.jar"] \
        or sorted(n for n in os.listdir(mods) if n.endswith((".jar", ".disabled"))) == [
            "a.jar", "b.jar", "c.jar.disabled", "d.jar", "user.jar"]
    assert open(os.path.join(mods, "b.jar"), "rb").read() == b"b"
    assert open(os.path.join(mods, "c.jar.disabled"), "rb").read() == b"c"
    assert open(os.path.join(game, "config", "x.toml")).read() == "x=1"
    assert open(os.path.join(game, "config", "mine.toml")).read() == "m=my"
    assert modpack_install.plan_repair(again, game).empty
    assert not [n for n in os.listdir(os.path.join(game, modpack_install.STATE_DIR)) if n.startswith("repair-")]


def test_same_version_for_curseforge_names():
    """У CurseForge версия в manifest короче названия файла — это не «новая версия»."""
    state = {"version": "1.24.1"}
    assert modpack_install.same_version(state, {"version": "FTB StoneBlock 4 1.24.1", "filename": "x.zip"})
    assert modpack_install.same_version(state, {"version": "", "filename": "ftb-stoneblock-4-1.24.1.zip"})
    assert not modpack_install.same_version(state, {"version": "FTB StoneBlock 4 1.24.10", "filename": "a-1.24.10.zip"})
    assert not modpack_install.same_version(state, {"version": "FTB StoneBlock 4 1.25.0", "filename": ""})
    assert modpack_install.same_version({"version": "x", "file_id": 7}, {"id": 7, "version": "y"})
    assert not modpack_install.same_version({"version": "1.0", "file_id": 7}, {"id": 8, "version": "1.0"})


def test_check_update_no_false_alarm(tmp_path, monkeypatch):
    os.makedirs(tmp_path / modpack_install.STATE_DIR)
    with open(modpack_install.state_path(str(tmp_path)), "w", encoding="utf-8") as f:
        json.dump({"project_id": "cf:1", "version": "1.24.1"}, f)
    monkeypatch.setattr(modpack_install, "latest_pack_file",
                        lambda p: {"version": "FTB StoneBlock 4 1.24.1", "filename": "ftb-1.24.1.zip", "id": 5})
    assert modpack_install.check_update(str(tmp_path)) is None
    monkeypatch.setattr(modpack_install, "latest_pack_file",
                        lambda p: {"version": "FTB StoneBlock 4 1.25.0", "filename": "ftb-1.25.0.zip", "id": 6})
    assert modpack_install.check_update(str(tmp_path))["latest"]["id"] == 6


def test_manual_files_picked_from_downloads(tmp_path):
    """Файлы «только с сайта», скачанные браузером, программа берёт из «Загрузок» — если совпала контрольная сумма."""
    import hashlib
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "good.jar").write_bytes(b"good")
    (downloads / "fake.jar").write_bytes(b"other")
    good = modpack_install.PackFile("mods/good.jar", None, {"sha1": hashlib.sha1(b"good").hexdigest()})
    fake = modpack_install.PackFile("mods/fake.jar", None, {"sha1": hashlib.sha1(b"real").hexdigest()})
    pack = modpack_install.PackFile("resourcepacks/Pipes.zip", None, {})
    (downloads / "Pipes.zip").write_bytes(b"zip")
    picked = modpack_install.pick_manual_downloads([good, fake, pack], str(tmp_path / "game"), str(downloads))
    assert picked == [good, pack]
    assert (tmp_path / "game" / "mods" / "good.jar").read_bytes() == b"good"
    assert not (tmp_path / "game" / "mods" / "fake.jar").exists() and (downloads / "good.jar").exists()
