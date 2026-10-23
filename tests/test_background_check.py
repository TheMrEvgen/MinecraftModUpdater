"""Фоновая проверка обновлений во всех сборках: что считать новым, сводка, устойчивость к ошибкам (без сети)."""
import background_check
from background_check import ProfileUpdates
from enums import ModStatus


def update(pid, version):
    return {"project_id": pid, "new_version": version, "status": ModStatus.UPDATE_AVAILABLE, "download_url": "x"}


def test_new_updates_only_unseen():
    results = [ProfileUpdates("A", [update("sodium", "0.6"), update("lithium", "0.14")]),
               ProfileUpdates("B", [update("iris", "1.8")])]
    notified = {"A": ["lithium:0.14", "sodium:0.6"]}
    fresh = background_check.new_updates(results, notified)
    assert [r.name for r in fresh] == ["B"]
    # Вышла ещё одна версия уже показанного мода — это снова новое
    results[0].updates[0] = update("sodium", "0.7")
    assert [r.name for r in background_check.new_updates(results, notified)] == ["A", "B"]


def test_summary_limits_names():
    results = [ProfileUpdates(f"Сборка {i}", [update("m", str(i))] * i) for i in range(1, 6)]
    text = background_check.summary(results, limit=2)
    assert text.startswith("«Сборка 1»: 1, «Сборка 2»: 2") and "3" in text.rsplit(" ", 1)[-1]
    assert background_check.summary([ProfileUpdates("Пусто")]) == ""


def test_collect_updates_skips_pinned_and_disabled():
    from view_model import pin_key
    pinned_mod = update("pinned", "2")
    mods = [update("a", "1"), pinned_mod, {**update("off", "1"), "disabled": True},
            {**update("b", "1"), "dependencies": [{**update("dep", "1"), "status": ModStatus.REQUIRED}]}]
    names = {u["project_id"] for u in background_check.collect_updates(mods, include_deps=False,
                                                                       pinned=[pin_key(pinned_mod)])}
    assert names == {"a", "b"}
    with_deps = {u["project_id"] for u in background_check.collect_updates(mods, include_deps=True)}
    assert {"dep", "pinned"} <= with_deps


def test_check_all_survives_broken_profile(monkeypatch):
    def fake(name, profile, cache_file=None, is_cancelled=lambda: False):
        if name == "broken":
            raise OSError("диск отвалился")
        return None if name == "no-folder" else ProfileUpdates(name, [update("x", "1")])

    monkeypatch.setattr(background_check, "check_profile", fake)
    config = {"profiles": {"broken": {}, "ok": {}, "no-folder": {}}}
    assert [r.name for r in background_check.check_all(config)] == ["ok"]
    assert background_check.check_all(config, is_cancelled=lambda: True) == []


def test_check_profile_without_folder(tmp_path):
    assert background_check.check_profile("Пусто", {"mods_path": ""}) is None
    assert background_check.check_profile("Нет папки", {"mods_path": str(tmp_path / "missing")}) is None
