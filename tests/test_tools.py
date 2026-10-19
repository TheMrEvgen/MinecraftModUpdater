"""Поиск с условиями, разбор краш-логов, бисекция, обслуживание, статистика, перенос настроек, акценты."""
import datetime
import json
import os
import zipfile

from enums import ModStatus


def _jar(path, mod_id="m", name="Mod"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("fabric.mod.json", json.dumps({"id": mod_id, "name": name, "version": "1"}))
    return str(path)


def _iso(days_ago: int) -> str:
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_ago)).isoformat()


# --- Поиск с условиями ---

def test_query_conditions():
    from query import parse_query
    sodium = {"name": "Sodium", "categories": ["optimization"], "client_side": "required", "server_side": "unsupported",
              "size": 2 * 1024 * 1024, "filepath": "m/sodium.jar", "status": ModStatus.UP_TO_DATE,
              "project_updated": _iso(10), "_favorite": True}
    api = {"name": "Fabric API", "categories": ["library"], "client_side": "required", "server_side": "required",
           "size": 6 * 1024 * 1024, "filepath": "m/api.jar", "status": ModStatus.UPDATE_AVAILABLE,
           "project_updated": _iso(500), "_note": "не трогать"}
    mods = [sodium, api]

    def names(q):
        query = parse_query(q)
        return [m["name"] for m in mods if query.matches(m)]

    assert names("tag:оптим") == ["Sodium"]
    assert names("-tag:библиотека") == ["Sodium"]
    assert names("side:client") == ["Sodium"] and names("side:server") == ["Fabric API"]
    assert names("size>5mb") == ["Fabric API"] and names("size<5mb") == ["Sodium"]
    assert names("is:fav") == ["Sodium"] and names("is:note") == ["Fabric API"] and names("is:lib") == ["Fabric API"]
    assert names("age>1y") == ["Fabric API"] and names("is:abandoned") == ["Fabric API"]
    assert names("status:update") == ["Fabric API"]
    assert names("трогать") == ["Fabric API"]  # Заметка участвует в поиске
    assert names('"fabric api" side:server') == ["Fabric API"]
    assert names("unknown:x") == []  # Неизвестное условие — обычное слово
    assert parse_query("").empty


# --- Краш-логи ---

def test_crash_analysis_ranks_suspects(tmp_path):
    from crash_analysis import analyze_file
    sodium = {"name": "Sodium", "slug": "sodium", "filepath": _jar(tmp_path / "sodium-0.6.jar", "sodium")}
    iris = {"name": "Iris Shaders", "slug": "iris", "filepath": _jar(tmp_path / "iris-1.8.jar", "iris")}
    other = {"name": "AppleSkin", "slug": "appleskin", "filepath": _jar(tmp_path / "appleskin.jar", "appleskin")}
    report = tmp_path / "crash.txt"
    report.write_text("""---- Minecraft Crash Report ----
Description: Rendering overlay

java.lang.NullPointerException: Cannot invoke "Object.hashCode()"
\tat net.caffeinemc.mods.sodium.client.render.SodiumWorldRenderer.render(SodiumWorldRenderer.java:42)
Caused by: org.spongepowered.asm.mixin.transformer.throwables.MixinTransformerError: from mod iris
A detailed walkthrough of the error
-- Suspected Mods --
Details:
\tSuspected Mods: Sodium (sodium)
""", encoding="utf-8")
    result = analyze_file(str(report), [sodium, iris, other])
    assert [s.entry["name"] for s in result.suspects] == ["Sodium", "Iris Shaders"]
    assert "назван в «Suspected Mods»" in result.suspects[0].reasons
    assert result.summary[0] == "Description: Rendering overlay"


# --- Бисекция ---

def test_bisect_finds_culprit_respecting_dependencies():
    from bisect_session import BisectSession, dependency_closure
    mods = [f"m{i}.jar" for i in range(8)]
    requires = {"m5.jar": {"m0.jar"}, "m6.jar": {"m0.jar"}}  # m0 — библиотека для m5 и m6
    assert dependency_closure({"m0.jar"}, requires) == {"m0.jar", "m5.jar", "m6.jar"}

    for culprit in mods:
        session = BisectSession.start(mods, ["old.jar"], requires)
        guard = 0
        while not session.done and guard < 20:
            disabled = set(session.disabled_now)
            # Игра «падает», если виновник включён (и все его зависимости тоже включены)
            assert not any(deps & disabled for mod, deps in requires.items() if mod not in disabled)
            session.answer(problem_persists=culprit not in disabled)
            guard += 1
        assert session.culprit == culprit, culprit
        assert session.original_disabled == ["old.jar"]
    restored = BisectSession.from_dict(json.loads(json.dumps(session.to_dict())))
    assert restored.suspects == session.suspects


def test_bisect_shared_library_does_not_loop():
    from bisect_session import BisectSession
    mods = ["lib.jar", "a.jar", "b.jar"]
    session = BisectSession.start(mods, [], {"a.jar": {"lib.jar"}, "b.jar": {"lib.jar"}})
    assert set(session.suspects) - set(session.disabled_now)  # Выключены не все подозреваемые


# --- Обслуживание ---

def test_integrity_check(tmp_path):
    from maintenance import check_integrity
    _jar(tmp_path / "ok.jar")
    (tmp_path / "broken.jar").write_bytes(b"not a zip")
    (tmp_path / "empty.jar").write_bytes(b"")
    (tmp_path / "x.jar.part").write_bytes(b"123")
    result = check_integrity(str(tmp_path))
    assert [os.path.basename(p) for p in result["broken"]] == ["broken.jar"]
    assert [os.path.basename(p) for p in result["empty"]] == ["empty.jar"]
    assert [os.path.basename(p) for p in result["partial"]] == ["x.jar.part"]


def test_backups_usage_and_prune(tmp_path):
    from maintenance import backups_usage, prune_backups
    from updater import UpdateTransaction, create_snapshot, list_snapshots, list_transactions
    _jar(tmp_path / "a.jar")
    snapshot = create_snapshot(str(tmp_path), "update_all")
    # Жёсткая ссылка на живой файл места не занимает — считается только manifest.json
    assert snapshot and backups_usage(str(tmp_path))["snapshots"] < 1000
    for i in range(3):
        tx = UpdateTransaction(str(tmp_path))
        path = tmp_path / f"old{i}.jar"
        path.write_bytes(b"x" * 1000)
        tx.backup_file(str(path))
        old = datetime.datetime.now().timestamp() - 90 * 86400
        os.utime(tx.dir, (old, old))
    assert 3000 <= backups_usage(str(tmp_path))["transactions"] < 5000
    removed = prune_backups(str(tmp_path), max_age_days=30)
    assert removed == 2 and len(list_transactions(str(tmp_path))) == 1  # Последняя операция остаётся
    assert list_snapshots(str(tmp_path))


def test_clone_and_compare(tmp_path):
    from maintenance import clone_game_dir, compare_entries, preset_plan
    src = tmp_path / "src"
    (src / "mods" / "ModUpdater_Backups").mkdir(parents=True)
    (src / "config").mkdir()
    _jar(src / "mods" / "a.jar")
    (src / "config" / "a.json").write_text("{}")
    (src / "options.txt").write_text("fov:70")
    copied = clone_game_dir(str(src), str(tmp_path / "dst"))
    assert set(copied) == {"mods", "config", "options.txt"}
    assert not (tmp_path / "dst" / "mods" / "ModUpdater_Backups").exists()

    left = [{"name": "A", "project_id": "a", "filepath": "x/a-1.jar", "current_version": "1"},
            {"name": "B", "project_id": "b", "filepath": "x/b.jar", "current_version": "1"}]
    right = [{"name": "A", "project_id": "a", "filepath": "y/a-2.jar", "current_version": "2"},
             {"name": "C", "filepath": "y/c.jar"}]
    diff = compare_entries(left, right)
    assert [e["name"] for e in diff["only_left"]] == ["B"] and [e["name"] for e in diff["only_right"]] == ["C"]
    assert [(a["current_version"], b["current_version"]) for a, b in diff["different"]] == [("1", "2")]

    plan = preset_plan(left + [{"name": "D", "filepath": "x/d.jar.disabled", "disabled": True}], ["a", "d.jar"])
    assert [e["name"] for e in plan["disable"]] == ["B"] and [e["name"] for e in plan["enable"]] == ["D"]


# --- Статистика, перенос настроек, акценты ---

def test_stats():
    from stats import build_stats
    entries = [{"name": "A", "filepath": "a.jar", "size": 100, "categories": ["optimization"],
                "client_side": "required", "server_side": "unsupported", "project_updated": _iso(800)},
               {"name": "B", "filepath": "b.jar", "size": 300, "categories": ["library"], "disabled": True},
               {"name": "Missing", "status": ModStatus.REQUIRED}]
    stats = build_stats(entries)
    assert stats["count"] == 2 and stats["total_size"] == 400 and stats["disabled"] == 1 and stats["libraries"] == 1
    assert [e["name"] for e in stats["heaviest"]] == ["B", "A"]
    assert stats["by_category"][0][0] == "библиотека" and stats["abandoned"][0][0]["name"] == "A"


def test_export_import_config(tmp_path):
    import pytest
    import config
    cfg = config.default_config()
    cfg["mod_notes"] = {"p": "заметка"}
    cfg["favorites"] = ["p"]
    path = str(tmp_path / "settings.json")
    config.export_config(cfg, path)
    loaded = config.import_config(path)
    assert loaded["mod_notes"] == {"p": "заметка"} and loaded["favorites"] == ["p"]
    (tmp_path / "other.json").write_text("{}")
    with pytest.raises(ValueError):
        config.import_config(str(tmp_path / "other.json"))


def test_accent_tokens():
    from theme import make_tokens
    blue = make_tokens("dark", "blue")
    assert blue["accent"] == "#4c8dff" and blue["success"] == make_tokens("dark")["success"]
    assert make_tokens("light", "#ffee00")["accent_text"] == "#0b0d11"  # Тёмный текст на светлом акценте
    assert make_tokens("dark", "not-a-color")["accent"] == make_tokens("dark")["accent"]


def test_transactions_and_snapshots_get_unique_dirs(tmp_path):
    from updater import UpdateTransaction, create_snapshot
    _jar(tmp_path / "a.jar")
    txs = [UpdateTransaction(str(tmp_path)) for _ in range(3)]
    for tx in txs:
        tx.record_installed("a.jar")
    assert len({tx.dir for tx in txs}) == 3  # Метка времени одинаковая, папки — разные
    snaps = [create_snapshot(str(tmp_path)) for _ in range(3)]
    assert len({s["dir"] for s in snaps}) == 3
