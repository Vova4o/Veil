Проведён детальный аудит работы с SQLite в кодовой базе (`database.py`, `library.py`, `indexing.py`, `worker.py`, `job_store.py`).

---

### Что уже сделано хорошо (Сильные стороны)
1. **WAL-режим и базовые PRAGMA**: включён [WAL-режим](file:///f:/ComfyUI_windows_portable/comfy-meta-viewer/app/database.py#L108-L113) (`PRAGMA journal_mode=WAL`), `synchronous=NORMAL`, `foreign_keys=ON` и `temp_store=MEMORY`.
2. **Deferred Join в `get_images_page`**: в [database.py (строка 1665)](file:///f:/ComfyUI_windows_portable/comfy-meta-viewer/app/database.py#L1665) основной просмотрщик сначала фильтрует и пагинирует лёгкие `id`, и лишь затем подтягивает поля конкретной страницы.
3. **Cursor / Keyset пагинация**: реализована поддержка курсора `(cursor_mtime, cursor_id)` для бесконечной ленты.
4. **Составные индексы**: созданы грамотные составные индексы под сортировку по дате и типам медиа (`idx_images_folder_media_mtime_id`, `idx_images_media_mtime_id`, `idx_item_positions_scope`).
5. **Атомарные транзакции**: очереди задач ([job_queue](file:///f:/ComfyUI_windows_portable/comfy-meta-viewer/app/database.py#L2103)) и перемещения используют `RETURNING` и транзакционные обвязки.

---

### Выявленные проблемы и места для оптимизации

#### 1. Жизненный цикл соединений и избыточные PRAGMA
* **Проблема: Открытие нового соединения на каждый чих.**  
  В коде нет пула соединений или connection reuse (например, потоко-локального или контекстного соединения). Каждая функция вызывает `get_conn()` -> `sqlite3.connect()` -> выполняет 6 PRAGMA -> делает запрос -> закрывает соединение.
* **Проблема: Избыточные PRAGMA при каждом подключении.**  
  ```python
  conn.execute("PRAGMA journal_mode=WAL")
  conn.execute("PRAGMA wal_autocheckpoint=1000")
  ```
  `journal_mode=WAL` сохраняется в заголовке файла БД при инициализации (`init_db`). Выполнять его при каждом коннекте избыточно и создаёт лишние блокировки/проверки заголовка файла.
* **Проблема: Конфликт таймаутов busy_handler.**  
  В `sqlite3.connect(..., timeout=30.0)` задаётся 30 секунд, но сразу после этого вызывается `conn.execute("PRAGMA busy_timeout=5000")`, что сбрасывает таймаут ожидания лока до 5 секунд.
* **Место для улучшения: Не настроены ключевые параметры кэша и MMAP.**  
  * `PRAGMA cache_size = -64000`: размер кэша страниц SQLite по умолчанию всего ~2 МБ (`-2000`). Для каталога с тысячами элементов кэш желательно увеличить до 32–64 МБ.
  * `PRAGMA mmap_size = 268435456` (256 МБ): использование memory-mapped I/O на 64-битных ОС (особенно Windows) исключает копирование страниц в userspace и ускоряет чтение на порядок.

---

#### 2. Проблема хранения BLOB и B-Tree Overflow
* **Проблема: `original_data BLOB` хранился прямо в таблице `images`.**  
  При загрузке картинок через веб-интерфейс (`/api/upload`) бинарные данные (10–30+ МБ) записывались в колонку `original_data` таблицы `images`.
  *Размер страницы SQLite по умолчанию — 4096 байт.* Из-за тяжелых BLOB плотность B-Tree падала, а сканы таблицы вычитывали сотни мегабайт overflow-страниц.
  * **Решение (✅ Реализовано):** Создана отдельная таблица `asset_blobs (image_id INTEGER PRIMARY KEY REFERENCES images(id) ON DELETE CASCADE, data BLOB NOT NULL)`. В таблицу `images` добавлен 1-байтовый флаг `has_original_data INTEGER NOT NULL DEFAULT 0` с частичным индексом `idx_images_has_original_data WHERE has_original_data = 1`. Реализована потоковая отдача через `blobopen("asset_blobs", ...)`, каскадное удаление и автомиграция старых данных.
* **Проблема: Рудиментарная колонка `thumbnail_b64 TEXT`.**  
  Колонка оставалась в схеме и `INSERT`-запросах со времён ранних прототипов, хотя все превью отдаются с диска (`.cache/thumbnails/<id>.jpg`).
  * **Решение (✅ Реализовано):** Удалена из схемы `_INITIAL_SCHEMA_SQL`, из запросов `INSERT/UPDATE` в `insert_assets` и автоматически удаляется (`DROP COLUMN`) при инициализации БД.

---

#### 3. Нестыковка индексов и WHERE-условий (Index Invalidation)
* **Проблема: `COALESCE(i.rating, 0) = ?` блокирует использование индекса по рейтингу.**  
  В [database.py (строка 553)](file:///f:/ComfyUI_windows_portable/comfy-meta-viewer/app/database.py#L553) создан индекс:
  ```sql
  CREATE INDEX IF NOT EXISTS idx_images_rating_media_mtime 
  ON images(rating, media_type, file_mtime DESC, id DESC);
  ```
  Но в [get_images_page (строка 1542)](file:///f:/ComfyUI_windows_portable/comfy-meta-viewer/app/database.py#L1542) фильтр строится как:
  ```sql
  AND COALESCE(i.rating, 0) = ?
  ```
  В SQLite обёртка колонки в выражение `COALESCE(col, ...)` делает B-Tree индекс по `rating` неприменимым! Запрос деградирует до сканирования.
  * **Решение**: Либо делать индекс по выражению `CREATE INDEX ... ON images(COALESCE(rating, 0), ...)`, либо приводить условие к `(i.rating = ? OR (i.rating IS NULL AND ? = 0))`.

---

#### 4. Неэффективность в `library.py` (Overfetching и отсутствие Deferred Join)
* **Проблема: Отсутствие Deferred Join в `get_assets` (library).**  
  В отличие от `get_images_page`, функция [get_assets](file:///f:/ComfyUI_windows_portable/comfy-meta-viewer/app/library.py#L1285) делала прямую сортировку с выборкой всех полей.
  * **Решение (✅ Реализовано):** Реализован Deferred Join через подзапрос `({subquery}) page_ids`. Подзапрос сортирует и пагинирует лёгкие `id`, а основной запрос подтягивает полные атрибуты только для запрашиваемых элементов страницы (`per_page`).
* **Проблема: Перекачка гигантских JSON ради двух булевых флагов.**  
  В `get_assets`:
  ```python
  metadata_json = asset.pop("metadata_json", None)
  # парсился через json.loads только ради:
  asset["has_embedded_metadata"] = bool(...)
  asset["has_metadata"] = bool(...)
  ```
  На 80 картинок со сложными ComfyUI-воркфлоу это было 5–15+ МБ JSON-текста, который SQLite читал с диска, передавал в Python и разбирал через `json.loads` на каждый запрос страницы.
  * **Решение (✅ Реализовано):** Флаги `has_metadata`, `has_embedded_metadata` и `has_ai_annotations` теперь вычисляются непосредственно на стороне SQLite через безопасные выражения (`CASE WHEN json_valid(...) ... json_extract(...)`), возвращая 1-байтовые булевы значения без передачи и десериализации гигантских строк `metadata_json` и `ai_annotations_json` в Python.
* **Проблема: Загрузка полного `metadata_json` в `get_images_page` (`database.py`).**  
  Основной просмотрщик `get_images_page` загружал полный `metadata_json` для страницы только ради извлечения `prompt_parameters`.
  * **Решение (✅ Реализовано):** В `get_images_page` запрос извлекает только `json_extract(i.metadata_json, '$.prompt_parameters') AS prompt_parameters_json`, не вычитывая графы ComfyUI в Python и парся только компактный словарь параметров генерации.

---

#### 5. Медленные фильтры через `json_tree` (Table Scan на лету)
* В функциях `list_metadata_filters()` и `_model_family_condition()` ([library.py:L130, L167](file:///f:/ComfyUI_windows_portable/comfy-meta-viewer/app/library.py#L167)) фильтрация по нодам и семейству моделей выполняет функцию виртуальной таблицы `json_tree(...)` над колонкой `metadata_json` для каждой строки.
* При 10 000+ изображениях разбор дерева JSON на лету во время SQL-запроса приводит к задержкам от сотен миллисекунд до нескольких секунд.
  * **Решение**: Извлекать список типов нод и семейство модели (SD1.5, SDXL, Flux, Pony) на этапе индексации/парсинга и сохранять в нормализованную индексную таблицу связей (как сделано с `image_tags`).

---

#### 6. Фоновый воркер и реконсиляция (`worker.py` & `indexing.py`)
* **Проблема: Воркер коммитит каждый файл отдельно.**  
  В `worker.py` для каждого изображения:
  1. Берётся соединение, делается `SELECT ... LIMIT 1`.
  2. Генерируется превью.
  3. Берётся соединение, делается `UPDATE`, выполняется `commit()`.
  На 1,000 картинок это 2,000 открытий/закрытий соединений SQLite и 1,000 дисковых синхронизаций (fsync) коммитов WAL.
* **Проблема: N+1 при реконсиляции в `indexing.py`.**  
  В [indexing.py (строка 275)](file:///f:/ComfyUI_windows_portable/comfy-meta-viewer/app/indexing.py#L275) для каждого измененного файла вызывается `db.get_image_ids_by_rel_paths(folder_id, [rel_path])`, хотя ID уже загружен в словаре `old_records[rel_path]["id"]`.
* **Проблема: Вставка в цикле вместо `executemany`.**  
  В [database.py (строка 927)](file:///f:/ComfyUI_windows_portable/comfy-meta-viewer/app/database.py#L927) `insert_assets` использует цикл Python `for asset in assets: conn.execute(...)` вместо вызова оптимизированного на уровне C метода `conn.executemany(...)`.

---

### Резюме и план действий

| Область | Текущее состояние | Потенциальный эффект от оптимизации | Сложность |
| :--- | :--- | :--- | :--- |
| **Pragmas & Cache** | ✅ **Реализовано** (кэш 64 МБ, mmap 256 МБ, 30s busy_timeout, WAL в init_db) | **+30–50%** к скорости чтения страниц и списков | Низкая (10 строк) |
| **Фильтр рейтинга** | ✅ **Реализовано** (выраженческие индексы `COALESCE(rating, 0)`, авто-миграция) | **+2x–5x** к скорости фильтрации по рейтингу | Низкая |
| **Вынос BLOB из `images`** | ✅ **Реализовано** (таблица `asset_blobs`, флаг `has_original_data`, удалены рудименты) | **+3x–10x** к скорости сканирования таблицы `images` | Средняя |
| **Overfetching JSON** | ✅ **Реализовано** (Deferred Join в `get_assets`, флаги в SQL, точечный `json_extract`) | **-70%** трафика памяти и CPU при скроллинге галереи | Средняя |
| **Пакетность воркера и `executemany`** | 1 коммит на каждый файл | **+200–400%** к скорости обработки очереди воркера | Средняя |
| **Индексация нод/моделей** | `json_tree` сканирует дерево JSON на лету | Мгновенная фильтрация по моделям (вместо секундных задержек) | Выше среднего |
