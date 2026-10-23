"""Имитация Minecraft для проверки автопоиска: падает, если включён «виновный» мод, иначе доходит до меню."""
import os
import sys
import time

game_dir, culprit = sys.argv[1], sys.argv[2]
mods = os.path.join(game_dir, "mods")
os.makedirs(os.path.join(game_dir, "logs"), exist_ok=True)
log_path = os.path.join(game_dir, "logs", "latest.log")
jars = [f for f in os.listdir(mods) if f.endswith(".jar")]
handles = [open(os.path.join(mods, j), "rb") for j in jars]  # Как настоящая игра: файлы модов заняты
time.sleep(1.5)
with open(log_path, "w", encoding="utf-8") as log:
    log.write("[main/INFO]: Loading Minecraft with Fabric Loader\n")
    if any(culprit in j for j in jars):
        log.write("[main/ERROR]: Mixin apply failed modmenu.mixins.json:MixinTitleScreen\n")
        os.makedirs(os.path.join(game_dir, "crash-reports"), exist_ok=True)
        with open(os.path.join(game_dir, "crash-reports", f"crash-{time.time():.0f}.txt"), "w") as report:
            report.write("---- Minecraft Crash Report ----\nDescription: Initializing game\n")
    else:
        log.write("[Render thread/INFO]: Sound engine started\n")
time.sleep(2)
for handle in handles:
    handle.close()
