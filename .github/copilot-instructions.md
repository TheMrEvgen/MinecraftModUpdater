# Copilot / AI agent instructions — Minecraft Mod Updater

Краткое руководство для AI‑агента, чтобы быстро стать продуктивным в этом репозитории.

Архитектура и поток данных
- UI: `main.py` — PyQt6 приложение с классом `ModUpdaterApp`.
- Фоновые задачи: `ScanWorker` (сканирование и запрос Modrinth) и `UpdateWorker` (бэкап + скачивание).
- Логика API/файлов: `mod_updater_logic.py` — все сетевые вызовы и файловые операции.
- Поток: сканирование `.jar` → вычисление SHA1 (`get_file_hash`) → POST `/version_files` → по `project_id` GET `/project/{id}/version` → показать в таблице → при обновлении: `backup_and_remove_mod` → `download_mod`.

Ключевые файлы для правок
- `mod_updater_logic.py`: меняйте здесь поведение сетевых запросов, заголовков (`HEADERS`), retry/backoff.
- `main.py`: интерфейс, слот‑сигнал связка, логика отображения таблицы и обработка потоков.

Специфичные паттерны и соглашения
- Сетевые функции безопасно отлавливают `requests.RequestException` и возвращают `{}` или `None` и логируют `print()`; при изменении этого поведения нужно обновить и обработку в `main.py`.
- `HEADERS['User-Agent']` должен оставаться и
<!--
[PROMPT_SUGGESTION]Добавь в таблицу столбец с датой выхода новой версии мода.[/PROMPT_SUGGESTION]
[PROMPT_SUGGESTION]Можно ли добавить общий прогресс-бар, который бы показывал скачивание всех модов?[/PROMPT_SUGGESTION]
-->
нформативным (Modrinth требует валидный UA).
- Бэкап: старые файлы перемещаются в `mod_backups` рядом с модом; файл бэкапа перезаписывается, если уже существует.
- Потоки: `UpdateWorker.progress` эмиттит `(index, status)` где `index` — индекс в `mods_to_update`. `start_update_worker` создаёт `row_map` для соответствия индексов строк таблицы.

Запуск и зависимости
- Рекомендуемая версия Python: 3.8+ (рекомендуется 3.10+).
- Базовые зависимости: `PyQt6`, `requests`, `packaging`, `tomli`.
- Примеры команд:

```bash
pip install PyQt6 requests packaging tomli
python main.py
```

Советы при изменении кода
- Если меняете формат данных, возвращаемых `get_mods_info_from_hashes` или `get_latest_version`, обновите логику разбора в `ScanWorker.run`.
- При добавлении retry/backoff для сетевых вызовов: реализуйте в `mod_updater_logic.py` и сохраняйте интерфейс (возвращаемое значение и ошибки) для совместимости с UI.
- Для изменения поведения бэкапов учитывайте, что UI ожидает, что `backup_and_remove_mod` завершится до попытки скачивания (последовательность в `UpdateWorker.run`).

Полезные места в коде (ориентиры)
- Сканирование и построение `mods_data`: в `ScanWorker.run` (см. проверку `mods_info` и формирование `mod_entry`).
- Выбор основного файла для скачивания: `primary_file = next((f for f in latest_version_info['files'] if f['primary']), None)` — при смене API убедитесь, что этот поиск корректен.

Если что-то непонятно — спросите: укажите файл и функцию, и я опишу ожидаемые контракты и стороны взаимодействия.
