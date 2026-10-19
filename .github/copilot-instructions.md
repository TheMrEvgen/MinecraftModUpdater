# Copilot / AI agent instructions — Minecraft Mod Updater

Краткое руководство для AI-агента, чтобы быстро стать продуктивным в этом репозитории.

## Архитектура

Логика отделена от интерфейса: модули без Qt используются и GUI, и консольным режимом.

| Модуль | Назначение |
|---|---|
| `main.py` | Точка входа: GUI или CLI (если переданы флаги `--update-all`, `--check`, `--profile`...) |
| `main_window.py` | Главное окно (`ModUpdaterApp`) — контроллер: сборки, операции, импорт/экспорт, drag & drop |
| `ui/mod_list.py` | Список модов: `ModListModel` + `ModFilterProxy` + `ModDelegate` (вся строка рисуется делегатом) |
| `ui/details_panel.py` | Правая панель мода: changelog, зависимости, другие версии |
| `ui/widgets.py` | Боковая панель, карточки-фильтры, чипы, уведомления (toasts), панель выделения, пустые состояния |
| `ui/pages.py` | Страницы «История» (снимки и операции), «Журнал», «Настройки» |
| `ui/catalog.py` | Страница «Каталог»: поиск на Modrinth (`/search`) и установка с обязательными зависимостями |
| `ui/palette.py` | Быстрые команды по Ctrl+K: действия, сборки, моды |
| `ui/dialogs.py` | «Что нового» перед обновлением, переход на другую версию игры, ввод текста |
| `content.py` | Типы контента: моды, шейдеры, ресурспаки (папка, расширения, `loaders` для Modrinth), `*.disabled` |
| `taskbar.py` | Прогресс на значке в панели задач Windows (ITaskbarList3 через ctypes) |
| `ui_components.py` | Диалоги (в т.ч. `ProfileDialog`), хелперы `show_message` / `ask_choice` / `confirm` |
| `theme.py` | Дизайн-токены тёмной/светлой темы, генерация QSS, тёмный заголовок окна Windows |
| `icons.py` | Векторные иконки (SVG), перекрашиваются под тему: `icons.icon(name, color)` |
| `view_model.py` | Строки списка (плоский / только мои / дерево), категории, группы, метки, известные конфликты, закрепление (без Qt) |
| `launchers.py` | Автопоиск сборок: Prism/MultiMC, Modrinth App, CurseForge, официальный лаунчер |
| `workers.py` | Тонкие `QThread`-обёртки над ядром + `IconLoader` (асинхронные иконки с кэшем) |
| `scanner.py` | Ядро сканирования: индекс файлов (SQLite-кэш по path/size/mtime), запросы к Modrinth, дерево зависимостей |
| `updater.py` | Установка, транзакции для отката, снимки сборки (жёсткие ссылки), выключение `*.disabled`, проверка блокировки файлов |
| `mod_updater_logic.py` | HTTP: Modrinth API, версии загрузчиков, скачивание с проверкой хэша |
| `exporters.py` | Строка «Поделиться», JSON для сервера, `.mrpack` |
| `game_detection.py` | Определение версии игры и загрузчика по папке `versions` |
| `config.py` | Пути (platformdirs), атомарное сохранение конфига, логирование с ротацией |
| `enums.py` | `ModStatus` (str-Enum со статусами для отображения), `UpdateChannel` |
| `cli.py` | Консольный режим для серверов и планировщика |

## Поток данных сканирования

`.jar` → SHA1 (кэшируется) → `POST /v2/version_files` (информация об установленной версии, кэшируется в SQLite)
→ **один** `POST /v2/version_files/update` с `loaders`, `game_versions`, `version_types` (последние версии всех модов)
→ недостающие зависимости: `GET /project/{id}/version` → `GET /v2/projects?ids=` (названия, иконки, client/server side).

## Соглашения

- Статусы — только через `ModStatus`, не строковыми литералами. `ModStatus` наследует `str`, поэтому хранится в словарях модов как есть.
- Новые сетевые вызовы — в `mod_updater_logic.py` через `SESSION` (там настроены повторы для 429/5xx; 4xx не повторяются). `HEADERS['User-Agent']` должен оставаться информативным (Modrinth требует валидный UA).
- Любое изменение папки `mods` выполняется через `UpdateTransaction` (бэкап в `mods/ModUpdater_Backups/transactions/<timestamp>` + `manifest.json`), чтобы его можно было откатить.
- Имена файлов из API проходят через `safe_filename()`; скачанные файлы проверяются по `sha512`/`sha1`.
- Перед изменением файлов проверяйте `find_locked_files()` / `is_game_running()`.
- Блокирующие операции в GUI выполняйте в потоках (`TaskWorker` для разовых запросов), не в UI-потоке.
- Воркеры сканирования/импорта отправляют `finished(None)` при отмене — UI сохраняет прежние данные.
- Цвета в коде интерфейса — только из токенов темы (`self.tokens[...]`), не литералами; после смены темы
  виджеты получают `set_tokens()`. Иконки кнопок задаются через `ui.widgets.button(..., icon_name=...)`,
  чтобы они перекрашивались автоматически.
- Записи модов — словари; из Qt-моделей их берут через `ui.mod_list.entry_at(index)`, а в сигналах передают
  как `pyqtSignal(object)`: `index.data()` и `pyqtSignal(dict)` отдают **копию** (QVariantMap), изменения в ней теряются.
- Шейдеры и ресурспаки сканируются тем же `scan_mods(..., content=SHADERS)`; у них нет зависимостей.
- Закрепление версий хранится в профиле (`profile["pinned"]`, ключ — `view_model.pin_key`), метки пользователя —
  в `config["mod_tags"]`. Закреплённые и выключенные моды не попадают в «Обновить все» (и в CLI `--update-all`).
- Версии Minecraft бывают в двух форматах: `1.21.1` и годовом `26.2` (с 2026) — см. `GAME_VERSION_RE`.

## Запуск, тесты, сборка

```bash
pip install -r requirements-dev.txt
python main.py                      # GUI
python main.py --list-profiles      # CLI
python main.py --profile Server --check
python -m pytest -q                 # тесты (без сети)
build.bat                           # тесты + сборка .exe (build.bat --onedir — сборка папкой)
```

Python 3.10+ (рекомендуется 3.12).
