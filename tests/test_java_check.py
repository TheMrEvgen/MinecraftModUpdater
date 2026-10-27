"""Java и память: нужная версия Java, рекомендация памяти, чтение и правка настроек лаунчера, разбор вылета."""
import json
import os

import java_check
from crash_analysis import diagnose


def test_required_java_and_versions():
    assert java_check.required_java("1.21.11") == (21, None)
    assert java_check.required_java("1.20.5") == (21, None)
    assert java_check.required_java("1.20.4") == (17, None)
    assert java_check.required_java("1.17.1") == (16, None)
    assert java_check.required_java("26.2") == (25, None)
    assert java_check.required_java("1.12.2", "forge") == (8, 8)
    assert java_check.required_java("1.12.2", "fabric") == (8, None)
    assert java_check.required_java("24w14a") is None
    assert java_check.java_major("21.0.7") == 21 and java_check.java_major("1.8.0_392") == 8
    assert java_check.parse_xmx("-Xmx4G -XX:+UseG1GC") == 4096 and java_check.parse_xmx("-Xmx3072m") == 3072
    assert java_check.parse_xmx("-XX:+UseG1GC") is None


def test_recommended_memory():
    assert java_check.recommended_memory(0) == 2048
    assert java_check.recommended_memory(200) == 6144
    assert java_check.recommended_memory(2000) == java_check.MAX_RECOMMENDED_GB * 1024
    assert java_check.recommended_memory(200, total_mb=8192) == 5 * 1024  # 3 ГБ оставляем системе
    assert java_check.recommended_memory(200, total_mb=4096) == 2048


def test_check_issues():
    profile = {"game_version": "1.21.1", "loader": "fabric"}
    wrong_java = java_check.Settings("prism", memory_mb=8192, java=17)
    issues = java_check.check(profile, 10, wrong_java, total_mb=32768)
    assert [i.kind for i in issues] == ["java"] and "17" in issues[0].text and issues[0].fix
    assert java_check.check(profile, 10, java_check.Settings("prism", 8192, 17, java_auto=True), 32768) == []
    low = java_check.check(profile, 200, java_check.Settings("prism", 2048, 21), total_mb=32768)
    assert [(i.kind, i.value) for i in low] == [("memory", 6144)]
    high = java_check.check(profile, 10, java_check.Settings("official", 15360, None, True), total_mb=16384)
    assert [i.kind for i in high] == ["memory_high"]


def _prism(tmp_path, instance_cfg: str, global_cfg: str):
    root = tmp_path / "PrismLauncher"
    game = root / "instances" / "Pack" / "minecraft"
    game.mkdir(parents=True)
    (root / "instances" / "Pack" / "instance.cfg").write_text(instance_cfg, encoding="utf-8")
    (root / "prismlauncher.cfg").write_text(global_cfg, encoding="utf-8")
    return {"source": "prism", "mods_path": str(game), "game_version": "1.21.1", "loader": "fabric"}


def test_prism_settings_and_fixes(tmp_path):
    profile = _prism(tmp_path,
                     "[General]\nname=Pack\nOverrideJavaLocation=true\nJavaPath=C:/jdk17/bin/javaw.exe\n"
                     "JavaVersion=17.0.9\nOverrideMemory=false\n",
                     "[General]\nAutomaticJavaSwitch=true\nMaxMemAlloc=3072\nJavaVersion=21.0.7\n")
    settings = java_check.read_settings(profile)
    assert (settings.memory_mb, settings.java, settings.java_auto) == (3072, 17, False)

    path, backup = java_check.set_memory(profile, 6144)
    text = open(path, encoding="utf-8").read()
    assert "OverrideMemory=true" in text and "MaxMemAlloc=6144" in text and "name=Pack" in text
    assert java_check.read_settings(profile).memory_mb == 6144

    java_check.reset_java(profile)
    settings = java_check.read_settings(profile)
    assert settings.java_auto and "JavaPath" not in open(path, encoding="utf-8").read()
    assert java_check.check(profile, 10, settings, total_mb=32768) == []

    java_check.restore(path, backup)  # «Отменить» возвращает файл как был
    assert java_check.read_settings(profile).memory_mb == 3072


def test_official_memory(tmp_path, monkeypatch):
    import official_launcher
    mc = tmp_path / ".minecraft"
    mc.mkdir()
    (mc / "launcher_profiles.json").write_text(json.dumps({"profiles": {
        "a": {"name": "A", "type": "custom", "lastVersionId": "x", "javaArgs": "-Xmx2G -XX:+UseG1GC"},
        "b": {"name": "B", "type": "custom", "lastVersionId": "x"}}}), encoding="utf-8")
    monkeypatch.setattr(official_launcher, "minecraft_dir", lambda: str(mc))
    for key in ("a", "b"):
        profile = {"source": "official", "launcher_id": key, "game_version": "1.21.1"}
        assert java_check.read_settings(profile).memory_mb == 2048
        java_check.set_memory(profile, 6144)
        assert java_check.read_settings(profile).memory_mb == 6144
    data = json.loads((mc / "launcher_profiles.json").read_text(encoding="utf-8"))
    assert data["profiles"]["a"]["javaArgs"] == "-Xmx6G -XX:+UseG1GC"
    assert data["profiles"]["b"]["javaArgs"].startswith("-Xmx6G -XX:")
    assert any(name.endswith(".bak") for name in os.listdir(mc))


def test_crash_diagnosis():
    old_java = ("java.lang.UnsupportedClassVersionError: net/minecraft/client/main/Main has been compiled by a more "
                "recent version of the Java Runtime (class file version 65.0), this version of the Java Runtime only "
                "recognizes class file versions up to 61.0")
    found = diagnose(old_java)
    assert [(d.kind, d.java) for d in found] == [("java", 21)] and "17" in found[0].text
    assert [d.kind for d in diagnose("Caused by: java.lang.IllegalArgumentException: Unsupported class file major version 65")] == ["java"]
    assert [d.kind for d in diagnose("java.lang.OutOfMemoryError: Java heap space")] == ["memory"]
    assert diagnose("java.lang.NullPointerException: oops") == []
