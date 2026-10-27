# Privacy Policy

*Русская версия — ниже.*

Minecraft Mod Updater does not collect, store or send any personal data or usage statistics. There is no telemetry,
no analytics and no accounts.

## What stays on your computer

Settings, modpack list, notes, tags, game launch history, cache and logs are stored only locally in
`%LOCALAPPDATA%\MinecraftModUpdater`. Nothing from this folder is uploaded anywhere.

## Network connections

The app connects to the following services only to do its job:

| Service | Why | What is sent |
|---|---|---|
| Modrinth (`api.modrinth.com`, `cdn.modrinth.com`) | Identify installed mods, check for updates, catalog, downloads | SHA-1 hashes of mod files, game version and loader, search queries |
| CurseForge (`api.curseforge.com`, `edge.forgecdn.net`) | Same, for mods not available on Modrinth | File fingerprints (MurmurHash2) of mod files, game version and loader, search queries |
| GitHub (`api.github.com`, `github.com`) | Check for new versions of the app; updates of mods published only on GitHub | Only requests for lists of releases (of the app and of linked mod repositories) |
| Google Translate (`translate.googleapis.com`) | Translating mod descriptions and changelogs — only when you press “Translate” or turn on automatic translation | The public description or changelog text of the mod and your interface language |
| Fabric, Quilt, Forge, NeoForge metadata servers | Find the latest loader version | Game version |
| Prism Launcher metadata (`meta.prismlauncher.org`) | Library versions when changing a Prism instance's game version | Game version |
| Fabric, Forge, NeoForge downloads | Server pack: the server launcher or installer | Game and loader version |

Automatic checks (mod updates when a modpack is opened, background checks, app update checks) can be turned off in
Settings. Mod files themselves are never uploaded — only their hashes.

The "Report on GitHub" button in the crash window opens a pre-filled issue in your browser; nothing is sent unless
you submit it yourself.

These services have their own privacy policies.

---

# Политика конфиденциальности

Minecraft Mod Updater не собирает, не хранит и не отправляет личные данные и статистику использования. Нет телеметрии,
аналитики и учётных записей.

**Что остаётся на компьютере.** Настройки, список сборок, заметки, метки, история запусков игры, кэш и журналы хранятся
только локально в `%LOCALAPPDATA%\MinecraftModUpdater` и никуда не отправляются.

**Сетевые подключения** — только для работы программы: Modrinth и CurseForge (хэши/отпечатки файлов модов, версия игры
и загрузчика, поисковые запросы), GitHub (проверка и загрузка новых версий программы, обновления модов, которые выходят только на GitHub), Google Переводчик
(только по кнопке «Перевести» или с включённым автопереводом: публичный текст описания или списка изменений мода и язык
интерфейса), серверы Fabric, Quilt, Forge,
NeoForge и Prism Launcher (версии загрузчиков и библиотек). Сами файлы модов никуда не загружаются — только их хэши.
Автоматические проверки можно выключить в настройках. Кнопка «Сообщить на GitHub» только открывает заполненную форму
в браузере — отправляете её вы сами.
