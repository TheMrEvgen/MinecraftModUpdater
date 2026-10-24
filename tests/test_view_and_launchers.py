import json
import zipfile

import pytest

from enums import ModStatus
from launchers import find_curseforge_instances, find_prism_instances
from scanner import mrpack_game_info, read_mrpack
from view_model import (
    CAT_INSTALL, CAT_NOT_FOUND, CAT_PROBLEMS, CAT_UPDATES, build_view_items, category_counts, subtitle_for,
)


def _mods_data():
    fabric_api_missing = {"name": "Fabric API", "project_id": "P7dR8mSH", "status": ModStatus.REQUIRED,
                          "type": "required", "filepath": None, "download_url": "u"}
    installed_dep = {"name": "Cloth Config", "project_id": "cloth", "status": ModStatus.UP_TO_DATE,
                     "type": "required", "filepath": "/m/cloth.jar", "installed": True}
    return [
        {"name": "Mod Menu", "project_id": "mm", "status": ModStatus.UPDATE_AVAILABLE, "filepath": "/m/mm.jar",
         "download_url": "u", "dependencies": [fabric_api_missing, installed_dep]},
        {"name": "Other", "project_id": "o", "status": ModStatus.UP_TO_DATE, "filepath": "/m/o.jar",
         "dependencies": [dict(installed_dep)]},
        {"name": "local", "project_id": None, "status": ModStatus.NOT_FOUND, "filepath": "/m/local.jar"},
    ]


def test_build_view_items_flattens_and_dedupes():
    items = build_view_items(_mods_data())
    names = [e["name"] for e in items]
    assert names == ["Mod Menu", "Other", "local", "Fabric API", "Cloth Config"]
    cloth = next(e for e in items if e["name"] == "Cloth Config")
    assert cloth["_required_by"] == ["Mod Menu", "Other"]
    mod_menu = items[0]
    assert mod_menu["_issues"] == ["нужен Fabric API"]


def test_category_counts():
    counts = category_counts(build_view_items(_mods_data()))
    assert counts[CAT_UPDATES] == 1
    assert counts[CAT_PROBLEMS] == 2  # Mod Menu (нет зависимости) и сама недостающая Fabric API
    assert counts[CAT_NOT_FOUND] == 1
    assert counts[CAT_INSTALL] == 1


def test_subtitles():
    items = build_view_items(_mods_data())
    text, role = subtitle_for(items[0])
    assert text.startswith("⚠ Нужен Fabric API") and role == "warning"
    fabric = next(e for e in items if e["name"] == "Fabric API")
    assert subtitle_for(fabric) == ("Нужен для: Mod Menu", "warning")
    assert subtitle_for(next(e for e in items if e["name"] == "local"))[0] == "local.jar"


def test_find_prism_instances(tmp_path):
    inst = tmp_path / "instances" / "MyPack"
    (inst / "minecraft" / "mods").mkdir(parents=True)
    (inst / "instance.cfg").write_text("InstanceType=OneSix\nname=Мой пак\n", encoding="utf-8")
    (inst / "mmc-pack.json").write_text(json.dumps({"components": [
        {"uid": "net.minecraft", "version": "26.2"},
        {"uid": "net.fabricmc.fabric-loader", "version": "0.19.5"}]}), encoding="utf-8")
    (tmp_path / "instances" / "broken").mkdir()
    found = find_prism_instances(str(tmp_path))
    assert len(found) == 1
    i = found[0]
    assert (i.name, i.game_version, i.loader, i.loader_version) == ("Мой пак", "26.2", "fabric", "0.19.5")
    profile = i.to_profile("release")
    assert profile["manual_mode"] and profile["source"] == "prism"


def test_find_curseforge_instances(tmp_path):
    inst = tmp_path / "Create"
    inst.mkdir()
    (inst / "minecraftinstance.json").write_text(json.dumps({
        "name": "Create Pack", "baseModLoader": {"name": "forge-47.2.0", "minecraftVersion": "1.20.1"}}), encoding="utf-8")
    found = find_curseforge_instances(str(tmp_path))
    assert (found[0].name, found[0].loader, found[0].loader_version, found[0].game_version) == \
        ("Create Pack", "forge", "47.2.0", "1.20.1")


def test_read_mrpack(tmp_path):
    path = tmp_path / "pack.mrpack"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("modrinth.index.json", json.dumps({
            "formatVersion": 1, "files": [], "dependencies": {"minecraft": "1.21.1", "neoforge": "21.1.5"}}))
    index = read_mrpack(str(path))
    assert mrpack_game_info(index) == ("1.21.1", "neoforge")
    bad = tmp_path / "bad.mrpack"
    bad.write_bytes(b"nope")
    with pytest.raises(ValueError):
        read_mrpack(str(bad))


def test_entry_at_returns_original_dict_through_proxy():
    """index.data() отдаёт копию dict — отметки должны меняться в самой записи модели."""
    from ui.mod_list import ModFilterProxy, ModListModel, entry_at
    model, proxy = ModListModel(), ModFilterProxy()
    items = [{"name": "B", "status": "x"}, {"name": "A", "status": "x"}]
    model.set_items(items)
    proxy.setSourceModel(model)
    proxy.set_sort_mode(ModFilterProxy.SORT_NAME)
    entry = entry_at(proxy.index(0, 0))
    assert entry is items[1]
    entry["_is_checked"] = True
    assert items[1]["_is_checked"] is True


def _tree_data():
    from enums import ModStatus
    api = {"name": "Fabric API", "project_id": "api", "filepath": "mods/api.jar", "status": ModStatus.UP_TO_DATE,
           "type": "required"}
    api2 = dict(api)
    missing = {"name": "Cloth", "project_id": "cloth", "filepath": None, "status": ModStatus.REQUIRED, "type": "required"}
    optional = {"name": "ModMenu", "project_id": "mm", "filepath": None, "status": ModStatus.OPTIONAL, "type": "optional"}
    a = {"name": "Alpha", "project_id": "a", "filepath": "mods/a.jar", "status": ModStatus.UP_TO_DATE,
         "dependencies": [api, missing]}
    b = {"name": "Beta", "project_id": "b", "filepath": "mods/b.jar", "status": ModStatus.UPDATE_AVAILABLE,
         "dependencies": [api2, optional]}
    return [a, b]


def test_build_rows_modes():
    from view_model import VIEW_FLAT, VIEW_MINE, VIEW_TREE, build_rows
    data = _tree_data()
    flat, counts = build_rows(data, VIEW_FLAT)
    assert [e["name"] for e, _ in flat] == ["Alpha", "Beta", "Fabric API", "Cloth"]
    mine, counts = build_rows(data, VIEW_MINE)
    assert [e["name"] for e, _ in mine] == ["Alpha", "Beta", "Fabric API"] and len(counts) == 3
    tree, counts = build_rows(data, VIEW_TREE)
    assert [(e["name"], m["depth"]) for e, m in tree] == [
        ("Alpha", 0), ("Cloth", 1), ("Fabric API", 1), ("Beta", 0), ("Fabric API", 1), ("ModMenu", 1)]
    assert tree[2][0] is tree[4][0]  # Общая зависимость — одна запись: отметка и статус общие
    assert tree[3][1]["children"] == [4, 5] and tree[5][1]["dep_type"] == "optional"
    assert len(counts) == 4  # Карточки считают моды без повторов, как в плоском списке


def test_proxy_tree_expand_and_filter():
    from ui.mod_list import ModFilterProxy, ModListModel, entry_at
    from view_model import CAT_UPDATES, VIEW_TREE, build_rows
    model, proxy = ModListModel(), ModFilterProxy()
    model.set_rows(build_rows(_tree_data(), VIEW_TREE)[0])
    proxy.setSourceModel(model)
    proxy.set_sort_mode(ModFilterProxy.SORT_NAME)

    def names():
        return [entry_at(proxy.index(r, 0))["name"] for r in range(proxy.rowCount())]

    assert names() == ["Alpha", "Beta"]
    proxy.set_expanded(model.entry(3), True)
    proxy.sort(0)
    assert names() == ["Alpha", "Beta", "Fabric API", "ModMenu"]
    proxy.set_expand_all(True)
    proxy.sort(0)
    assert names() == ["Alpha", "Cloth", "Fabric API", "Beta", "Fabric API", "ModMenu"]
    proxy.set_expand_all(False)
    proxy.set_filter(query="cloth")
    assert names() == ["Alpha", "Cloth"]  # Найденная зависимость видна вместе со своим модом
    proxy.set_filter(category=CAT_UPDATES, query="")
    assert names() == ["Beta"]


def test_legacy_launcher_config(tmp_path):
    import json

    from launchers import find_legacy_instances, read_properties
    install = tmp_path / "Legacy"
    install.mkdir()
    game = tmp_path / "game dir"
    version = "fabric-loader-0.18.4-1.21.11"
    (game / "versions" / version).mkdir(parents=True)
    (game / "mods").mkdir()
    (game / "versions" / version / f"{version}.json").write_text(json.dumps(
        {"id": version, "inheritsFrom": "1.21.11", "libraries": []}), encoding="utf-8")
    escaped = str(game).replace("\\", "\\\\").replace(":", "\:")
    (install / "tl.properties").write_text(f"#comment\nminecraft.gamedir={escaped}\nlogin.version={version}\n"
                                           "minecraft.gamedir.separate=none\n", encoding="utf-8")
    assert read_properties(str(install / "tl.properties"))["minecraft.gamedir"] == str(game)
    found = find_legacy_instances(appdata=str(tmp_path / "appdata"), installs=[str(install)])
    assert len(found) == 1 and found[0].source == "legacy" and found[0].game_dir == str(game)
    assert (found[0].game_version, found[0].loader, found[0].loader_version) == ("1.21.11", "fabric", "0.18.4")
    assert find_legacy_instances(appdata=str(tmp_path / "appdata"), installs=[]) == []
