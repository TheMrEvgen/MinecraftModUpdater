"""Какой загрузчик нужен модам: условия Fabric, диапазоны Forge/NeoForge, проверка папки и разбор вылета."""
import json
import zipfile

import loader_check
from crash_analysis import diagnose


def test_predicates_and_ranges():
    assert loader_check.min_from_predicate(">=0.20.1") == "0.20.1"
    assert loader_check.min_from_predicate("^0.16.0") == "0.16.0"
    assert loader_check.min_from_predicate(">=0.15.0 <0.17") == "0.15.0"
    assert loader_check.min_from_predicate("0.15.x") == "0.15"
    assert loader_check.min_from_predicate(["0.15.x", ">=0.16"]) == "0.15"
    assert loader_check.min_from_predicate(["*", ">=0.16"]) is None
    assert loader_check.min_from_predicate("*") is None and loader_check.min_from_predicate(None) is None
    assert loader_check.min_from_range("[21.1.77,)") == "21.1.77"
    assert loader_check.min_from_range("[47,)") == "47"
    assert loader_check.min_from_range("*") is None and loader_check.min_from_range("") is None


def fabric_jar(path, depends):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("fabric.mod.json", json.dumps({"id": path.stem, "depends": depends}))
    return {"filepath": str(path), "name": path.stem}


def test_check_folder(tmp_path):
    entries = [fabric_jar(tmp_path / "kotlin.jar", {"fabricloader": ">=0.19.5"}),
               fabric_jar(tmp_path / "extra.jar", {"fabricloader": ">=0.20.1", "minecraft": "1.21.11"}),
               fabric_jar(tmp_path / "old.jar", {"fabricloader": ">=0.14"}),
               fabric_jar(tmp_path / "off.jar", {"fabricloader": ">=0.99"})]
    entries[3]["disabled"] = True  # Выключенный мод загрузчику не мешает
    need = loader_check.check(entries, "fabric", "0.19.3")
    assert need.version == "0.20.1" and need.mods == ["extra", "kotlin"]
    assert loader_check.check(entries, "fabric", "0.20.1") is None
    assert loader_check.check(entries, "fabric", None) is None

    neo = tmp_path / "neo.jar"
    with zipfile.ZipFile(neo, "w") as z:
        z.writestr("META-INF/neoforge.mods.toml", 'modLoader="javafml"\n[[mods]]\nmodId="neo"\n'
                   '[[dependencies.neo]]\nmodId="neoforge"\ntype="required"\nversionRange="[21.1.77,)"\n')
    assert loader_check.required_version(str(neo), "neoforge") == "21.1.77"
    assert loader_check.check([{"filepath": str(neo), "name": "Neo"}], "neoforge", "21.1.50").version == "21.1.77"


def test_crash_diagnosis_old_loader():
    log = ("Some of your mods are incompatible with the game or each other!\n"
           "\t - Mod 'Fabric Language Kotlin' (fabric-language-kotlin) 1.14.2 requires version 0.19.5 or later of mod "
           "'Fabric Loader' (fabricloader), but only the wrong version is present: 0.19.3!\n"
           "\t - Mod 'Sodium Extra' (sodium-extra) 0.9.6 requires version 0.20.1 or later of mod 'Fabric Loader' "
           "(fabricloader), but only the wrong version is present: 0.19.3!\n")
    found = diagnose(log)
    assert [(d.kind, d.version) for d in found] == [("loader", "0.20.1")]
    assert "Sodium Extra" in found[0].text and "0.19.3" in found[0].text
    neo = diagnose("\tMod ID: 'neoforge', Requested by: 'sodium', Expected range: '[21.1.77,)', Actual version: '21.1.50'")
    assert [(d.kind, d.version) for d in neo] == [("loader", "21.1.77")]
