# Create на Krea2: три режима — план и состояние работ

> **Статус:** активный план реализации Create v1 (исследование и решения от 2026-10-10, этап 1 выполнен).
>
> Начинай новую сессию по Create отсюда. Документ собирает решения пользователя, результаты исследований из чата, сделанное в этапе 1 и чек-листы следующих этапов. Долгосрочное направление Studio описано в [EDITOR-STUDIO-RUNTIME-PLAN.md](EDITOR-STUDIO-RUNTIME-PLAN.md); как эти планы соотносятся — в разделе 2.

## 1. Решения пользователя

- **Одна модель:** `moodyKrea2Mix_v40.safetensors` (Moody Krea 2 Mix V4.0 FP8). Другие Krea2-чекпоинты (redcraft, lustify) требуют других графов и компонентов — вне рамок.
- **Три подготовленных режима**, никаких построителей графов и пользовательских флоу:
  1. **По референсу** (`reference`): фото + необязательные пожелания → агент пишет подробный промпт по референсу → txt2img.
  2. **Правка фото** (`edit`): фото + что изменить → агент пишет команду правки → граф Krea2 Identity Edit (LoRA + ноды `comfyui-krea2edit`).
  3. **По описанию** (`text`): только текст → агент пишет промпт → txt2img.
- **Пользователь выбирает только** соотношение сторон и количество изображений **1–4**. Все разрешения около **1 МП**. Никакого выбора моделей, LoRA, качества.
- **UI не изобретаем заново, а адаптируем текущий Create**; когда есть фото, слева показываем карточку «До», справа результаты «После» — для сравнения.
- **Агенты:** Direct (OpenAI-compatible), OpenCode, **Claude Code** и **Antigravity (`agy`)** — последние два нужно починить и научить принимать картинки.
- **Ноды** ставятся автоматически по кнопке «Скачать недостающее» (архив закреплённого коммита), плюс инструкция для ручной установки.
- **Кнопка «Улучшить детали»** на готовом кадре — да; недостающее скачивать самостоятельно.
- **Режим 2 с NSFW** пользователь проверял — работает; запасных маршрутов сейчас не делаем.
- **Инструкция агента:** на первом этапе одна мета-инструкция [`instruction.md`](../instruction.md) (проверена пользователем для режима 1), потом перенос на скиллы.

## 2. Связь с прежними планами и кодом

- [EDITOR-STUDIO-RUNTIME-PLAN.md](EDITOR-STUDIO-RUNTIME-PLAN.md) остаётся долгосрочным направлением (VisualSession, история, Agent Lab). Отличие Create v1: **режимы явные** (решение пользователя), а не выводятся рантаймом из инструкции; промпт пишет агент по `instruction.md`.
- В `app/visual_runtime/` уже реализованы шаги 1–5 того плана: `visual_sessions` / `visual_references` / `visual_actions` в SQLite, реестр примитивов `moody-generate` / `moody-edit` (статус `pending_graph`), API `/api/studio/*`. К UI это **не подключено**.
- **Рекомендация на старт этапа 2:** сессии и историю Create строить на `visual_runtime.store` (не заводить новые таблицы), а примитивы `moody-generate` / `moody-edit` направить на проверенные графы профиля `krea2_moody_v40` (раздел 4), чтобы не было двух источников правды.
- **Известное отступление:** этап 1 пишет `mode`, `seeds`, `user_request` в `workflow_drafts.values`; план Studio (§6) просит не перегружать drafts студийной семантикой — перенести в `visual_actions` на этапе 2.

## 3. Компоненты и источники

Все файлы уже лежат в `C:\Users\etotm\AppData\Local\Comfy-Desktop\ComfyUI-Shared\models` (ComfyUI Desktop 0.39.1, RTX 2070 Super 8 ГБ).

| Компонент | Папка / файл | Источник | SHA256 |
|---|---|---|---|
| Модель | `diffusion_models/moodyKrea2Mix_v40.safetensors` (12,8 ГБ, fp8) | Civitai `3117958`, `fileId=2998244` | `12d42c1f…feaa1ca` |
| Текстовый энкодер | `text_encoders/qwen3vl_4b_fp8_scaled.safetensors` (`CLIPLoader type=krea2`) | HF `Comfy-Org/Krea-2` | — |
| VAE | `vae/qwen_image_vae.safetensors` | HF `Comfy-Org/Krea-2` | — |
| LoRA правки | `loras/krea2_identity_edit_v1_2_r64.safetensors` (457 МБ) | Civitai `3139172`, `fileId=3025700` | `f794b471…d4242423` |
| LoRA разнообразия SDA | `loras/krea2_turbo_sda_v1.0_comfy.safetensors` (469 МБ) | HF `F16/krea2-turbo-sda` | `0fafed04…cdb66dba2b` |
| Ноды правки | `custom_nodes/comfyui-krea2edit` | GitHub `lbouaraba/comfyui-krea2edit` @ `86f886d` (v1.2.5), Apache-2.0, без зависимостей | `__init__.py`: `43c5f9aa…89c3d39` |

- Ссылка `modelVersionId=3134403` («V4.0 Converted FP8», 13,8 ГБ) — **другой файл** для WanGP/NEO, для ComfyUI не подходит.
- KJNodes **не нужен**: подгонку цвета делает нода ядра `ColorTransfer` (`mkl_lab`, `strength`).
- Ноды `comfyui-krea2edit` установлены вручную 2026-10-10 (с меткой `.cmv-node-pack.json`), но **ComfyUI ещё не перезапускался** — классы `Krea2EditModelPatch` / `Krea2EditGroundedEncode` пока не загружены.

## 4. Рецепты графов

Профиль: [`app/comfyui/simple_models/krea2_moody_v40/`](../app/comfyui/simple_models/krea2_moody_v40/).

### 4.1 txt2img (`workflow.json`, режимы `text` и `reference`)

- `UNETLoader` → `CLIPLoader (krea2)` → `CLIPTextEncode`; негатив — `ConditioningZeroOut`; латент `EmptySD3LatentImage`.
- Сэмплер **`dpmpp_2m_sde_gpu` + `sgm_uniform`, 8 шагов, CFG 1**.
- **SDA gate = 2:** `KSamplerAdvanced` шаги 0–2 на модели с LoRA SDA (`return_with_leftover_noise`), затем шаги 2–8 на базовой модели (`add_noise=disable`). Сила LoRA задаётся по режиму: `text` → 1.0, `reference` → 0.0 (композиция референса важнее разнообразия). При силе 0 ComfyUI пропускает LoRA, разбиение на два сэмплера сохраняет качество (проверено).
- Пакет 1–4 = **отдельные задачи в очереди** с сидами s, s+1…; картинки приходят по одной, на 8 ГБ это надёжнее латентного batch.
- Размеры: 1:1 → 1024×1024, 3:4 → 896×1152, 4:3 → 1152×896, 9:16 → 768×1344, 16:9 → 1344×768; `source` — пропорции фото при ~1 МП, шаг 32, ограничение 1:2.5…2.5:1 (`resolve_dimensions`).

### 4.2 Правка (`edit.workflow.json`, режим `edit`)

- `LoadImage` → `ImageScaleToTotalPixels` (1 МП, `resolution_steps=32`) → `VAEEncode`.
- `LoraLoaderModelOnly` identity edit r64 (1.0) → `Krea2EditModelPatch` (`ref_boost=4`, `ref_boost_a=1`, `fit_mode=fit`, `source_latent`, `source_image`, `target_latent`, `vae`).
- `Krea2EditGroundedEncode` позитив = команда правки, негатив = пустая команда с тем же фото, `grounding_px=768`.
- `KSampler` 8 шагов, CFG 1, `euler_ancestral` / `sgm_uniform`, denoise 1 на новом `EmptySD3LatentImage` (исходник — не стартовый латент, а отдельное условие).
- Рецепт взят из проверенного прогона `image_gen` (`research/local-runs/workflows/moodykrea2-local-identity-edit.api.json`); **после перезапуска ComfyUI откалибровать живьём** (раздел 10, этап 1b).
- Исходник загружается в ComfyUI через `ComfyUIClient.upload_image` (`input/cmv/cmv_source_<uuid>.png`).

### 4.3 «Улучшить детали» (ещё не реализовано)

Рецепт второго прохода автора Moody, адаптированный к готовому кадру:

```text
LoadImage(результат) -> VAEEncode -> LatentUpscaleBy(bicubic, 1.5)
-> KSampler(4 шага, CFG 1, euler_ancestral/beta, denoise ~0.39, тот же позитив)
-> VAEDecode -> ColorTransfer(mkl_lab, strength ~0.22, ref = исходный кадр) -> SaveImage
```

Выход ~2,25 МП. Промпт берётся из метаданных результата (`cmv_prompt`). Автор усиливает этот проход LoRA `krea2_turbo_4step_rank_64` (0.5) — её нет, начинать без неё. Позже — кнопка «4K» (SeedVR2, отдельный набор нод и тяжёлые модели).

## 5. Калибровка 2026-10-10

Промпт: портрет в кафе (кожа, веснушки, руки), 896×1152, сид 20261010, ~43–48 с на кадр. Картинки: `ComfyUI-Shared/output/CMV/Calibration/`. Скрипт: [`scripts/krea2_calibrate.py`](../scripts/krea2_calibrate.py).

| Вариант | Итог |
|---|---|
| A `euler_ancestral` / `beta` (как у автора Moody) | кожа глаже, блеск на носу/лбу |
| B `euler_ancestral` / `sgm_uniform` (как в `image_gen`) | почти как A |
| **C `dpmpp_2m_sde_gpu` / `sgm_uniform`** (как в v40-графе пользователя) | **самая естественная кожа, поры и веснушки — выбран** |
| C, 4 сида без SDA | почти один и тот же кадр (turbo-дистилляция) |
| C, 4 сида + SDA gate 2 | разные планы, позы, фон; качество и время те же |
| C + SDA 0 через профиль (`reference`) | та же композиция, что у одиночного сэмплера |

## 6. Исследование воркфлоу автора Moody

Файлы пользователя: `C:\Users\etotm\Downloads\moodyKrea2{Minimal_v40,Simple_v40,Advanced_v10,ImageToImage4KHD_v10}.json`, `moodyKrea24KHD_v20.json`, `moodyKrea2PromptWriting_v10.txt` (Civitai отдаёт их только залогиненным).

- Базовый рецепт всех v4-графов: 8 шагов, CFG 1, `euler_ancestral` / `beta`, `ConditioningZeroOut`; база 1 МП (Simple/Advanced), 1,5–2 МП (4K HD, Minimal, Img2Img).
- **Extra Details:** латентный апскейл ×1,5 → 4 шага denoise 0,39 → ColorMatch 0,22 (основа для 4.3).
- **SDA Variety Fix** (Advanced): первые 2 шага с LoRA SDA — так и сделано в 4.1.
- Тяжёлое (не для простого Create): UltimateSDUpscale, детейлеры Impact Pack (лицо, интимные зоны), ручной инпейнт, Skin Contrast, SeedVR2 4K. **Видео нет** — «Video» только в имени ноды `SeedVR2VideoUpscaler`.
- Img2Img 4KHD: автоописание Florence-2 + обычный img2img (denoise ≈0,49). Не берём: у нас режим 1 — агент. Заметка автора: denoise > ~0,2 даёт белёсые разводы на лице.
- **Промпт-скилл автора** (`moodyKrea2PromptWriting_v10.txt`): абзац про камеру/стиль/свет → по абзацу на каждого персонажа (где стоит → кто → внешность → одежда → поза с явными руками/ногами → связь с другими) → окружение → фокус и композиция; конкретика без эмоций и символизма, без дежурных «cinematic/golden hour/35mm», проверка физической согласованности, NSFW медицинскими терминами, вывод — только промпт.

## 7. Агент и инструкция

### 7.1 `instruction.md` (~61,5 КБ, ~15k токенов)

- Фазы: роутер SFW/NSFW → Scene Map (внутренне) → паспорт персонажа и непрерывность сессии → оптика/стиль/свет → субъект (идентичность, тело, выражение, пространство, текст) → гардероб → интимность → влажность → сериализация → итеративная доработка (delta rule, preservation anchor, cascading consequence) → примеры → ограничители.
- Вывод — **один блок кода** с английским промптом. Сервер извлекает текст из блока, а не заставляет модель отвечать JSON (не ломаем проверенное поведение).
- «T5-XXL» в тексте — историческое имя; у Krea2 энкодер Qwen3-VL, лимит T5 в 512 токенов не действует.

### 7.2 Надстройки по режимам (этап 2)

- `reference`: инструкция как есть; текст пользователя — запрошенная трансформация (в т.ч. триггер «hard»).
- `text`: Scene Map строится по описанию; правило «не выдумывать отсутствующего на фото» заменяется на «заполнять недостающее согласованными творческими решениями»; паспорт и series lock — по истории сессии.
- `edit`: формат **команды правки** (editor instruction): что меняется + явные цели сохранения (Phase 2A: радужка, волосы, макияж, ногти, аксессуары; Phase 3: материал/крой; палитра/экспозиция/фон), без «keep pose» при смене позы; без полного описания сцены. Образцы — `image_gen/skills/forma-muse/references/editing-prompts.md`.
- **A/B на одних референсах:** (а) `instruction.md` как есть, (б) та же инструкция с раскладкой абзацев по персонажам от автора Moody. Выбирает пользователь по картинкам.

### 7.3 Исполнители

| Адаптер | Картинки | Скиллы | Состояние |
|---|---|---|---|
| Direct (OpenAI-compatible) | `data_url` | — | работает |
| OpenCode | `file_path` | да | работает |
| Claude Code (`claude` → npm `claude.cmd`) | нет | нет | **сломан для большой инструкции** |
| Antigravity (`agy.exe`) | нет | да | **сломан для большой инструкции** |

- **Баг:** [`claude_code.py`](../app/ai/execution/claude_code.py) и [`antigravity.py`](../app/ai/execution/antigravity.py) передают всю инструкцию аргументом командной строки (`stdin=DEVNULL`). Лимиты: cmd.exe 8191 символ (`claude.cmd`), CreateProcess 32 767 (`agy.exe`); 61 КБ гарантированно падает.
- **Исправление:** промпт через stdin или файл в отдельной рабочей папке прогона; картинку класть файлом туда же (Claude Code читает изображения инструментом Read), выставить `supports_images=True` / `image_inputs=("file_path",)`; флаги `agy` для картинок проверить живым прогоном.
- Текущий путь референса в [`simple_routes._prepare_prompt`](../app/comfyui/simple_routes.py) слабый (`SceneAnalysisService` даёт лишь «тип объекта + фон») — заменить вызовом агента через `ExecutionRouter`.
- Без профиля со зрением режим `reference` должен честно сообщать об этом; в `edit` без зрения агент только оформляет команду (картинку и так видит Qwen3-VL внутри графа).
- Целевая скилл-практика (этап 5) — как в `F:\projects\image_gen\skills`: `image-new` / `image-ref` / `image-edit` поверх ядра `forma-muse` с референсами по фазам.

## 8. UI: адаптация текущего Create

Текущая разметка: [`create.html`](../app/templates/create.html), контроллер [`simple-mode-v2.js`](../app/static/js/features/simple-mode-v2.js) (~2000 строк), стили [`simple-mode.css`](../app/static/css/features/simple-mode.css): `.studio-layout[data-view=idle]` — одна карточка по центру; `generating`/`result` — карточка управления + холст справа (ресайз через `--canvas-column-width`), ниже 1050px — одна колонка.

- Над полем ввода — переключатель режимов «По референсу / Правка фото / По описанию»; плейсхолдер и обязательность фото по режиму.
- Есть фото → слева карточка **«До»**, центр на месте, справа **«После»**; обе крайние карточки одной ширины (общая ручка ресайза). В режиме «По описанию» — как сейчас, две колонки.
- Блок «Модель» с выпадающим списком → одна строка статуса (модель + готовность); при нехватке — панель «Скачать недостающее» (модели + ноды, сообщение о перезапуске ComfyUI, команда ручной установки из `manual_install`).
- Убрать «Качество», «Add details / Always add details»; «Изображений» 1–4; соотношения — пять пресетов + «Как у фото» в режимах с фото.
- «ИИ-помощник» в шапке → журнал сессии с агентом (промпты по ходам, «Доработать»).
- Действия на «После»: «Скачать», «Править это» (кадр становится новым «До» в режиме правки), «Доработать», «Промпт» (просмотр, ручная правка, «Перегенерировать» без агента), позже «Улучшить детали».
- Опрашивать **все** `run_ids` из ответа генерации; прогресс: «Агент пишет промпт… → 2 из 4».
- Иконки — локальные SVG (не эмодзи); фон ambient — из источников библиотеки (директивы проекта).

## 9. Сделано в этапе 1 (2026-10-10)

- Профиль `krea2_moody_v40` (сортируется первым → профиль по умолчанию): `manifest.json` (`modes`, `sda_modes`, `max_batch`, `quality_presets: []`), `resources.json` (SHA256 + `custom_nodes`), `workflow.json` + `bindings.json`, `edit.workflow.json` + `edit.bindings.json`, `prompt_style.md`.
- [`simple_profiles.py`](../app/comfyui/simple_profiles.py): `CustomNodeRequirement`, поля профиля, привязки со списком целей, `resolve_dimensions`, `compile_edit_workflow`, `has_edit_workflow`; health: `missing_nodes`, `nodes_known`, `restart_required`, `modes_ready` (нехватка нод блокирует только `edit`).
- [`node_packs.py`](../app/comfyui/node_packs.py): установка архива закреплённого коммита (защита от `../`, сверка SHA256 входного файла, staging + rename, метка `.cmv-node-pack.json`, чужие папки не трогает).
- [`simple_routes.py`](../app/comfyui/simple_routes.py): `POST /api/simple/models/<id>/install` ставит и ноды (`node_packs`, `restart_required`, `manual_install`); `POST /api/simple/generate` принимает `mode` (`text`/`reference`/`edit`), для профилей без пресетов — `_generate_fixed`: N задач с сидами подряд, загрузка исходника для правки, ответ с `run_ids`, `width`/`height`. Старые профили `model_01…08` идут прежним путём до этапа 3.
- Тесты: [`tests/test_create_krea2.py`](../tests/test_create_krea2.py) (17), обновлён `tests/test_simple_mode.py`; `lint.ps1` зелёный.
- Живые проверки: txt2img `text` (SDA) и `reference` (SDA 0) через скомпилированный граф профиля.

## 10. Дальше

### Этап 1b — калибровка правки (нужен перезапуск ComfyUI в Comfy Desktop)
- [ ] Убедиться, что `/object_info` отдаёт `Krea2EditModelPatch` и `Krea2EditGroundedEncode`.
- [ ] Прогнать правку на фото пользователя: `euler_ancestral/sgm_uniform` против `dpmpp_2m_sde_gpu/sgm_uniform`; `ref_boost` 4 против 2–6; `grounding_px` 768 против 512. Зафиксировать победителя в `edit.workflow.json`.
- [ ] Проверить «Как у фото» и пресеты соотношений на правке (`fit_mode=fit`).

### Этап 2 — агент
- [ ] Решить по разделу 2: сессии на `visual_runtime.store`, примитивы → графы профиля.
- [ ] Операции/надстройки режимов поверх `instruction.md`; извлечение промпта из блока кода; история сессии в запросе (паспорт, delta rule).
- [ ] Endpoint агента отдельно от генерации (`prompt` → `generate`), поток текста по мере написания (`on_output_chunk`).
- [ ] Починить Claude Code и `agy`: stdin/файл, картинки файлами; тесты на длинную инструкцию.
- [ ] A/B: `instruction.md` против раскладки по персонажам Moody.
- [ ] Метаданные результата: режим, запрос, промпт агента, id исходника (часть уже в `extra_pnginfo`).

### Этап 3 — UI
- [ ] Адаптировать Create по разделу 8; удалить `model_01…08`, легаси-ветку генерации, выбор модели и качества.
- [ ] Playwright-проверка (системный Edge `channel='msedge'`; скрытая панель браузера Claude не шлёт rAF/scroll).

### Этап 4 — «Улучшить детали» по 4.3, затем «4K» (SeedVR2).

### Этап 5 — перенос `instruction.md` на скиллы (ядро + фазы + режимы), прогрессивная загрузка для OpenCode/agy, собранный бандл для Direct.

## 11. Открытые вопросы

- Подтвердить правило SDA: вкл. для `text`, выкл. для `reference`.
- Перезапуск ComfyUI Desktop из CMV невозможен (процесс принадлежит Desktop) — в UI только сообщение; нужен ли автоматический путь через ComfyUI-Manager (`--enable-manager`), решить позже.
- После перезапуска CMV (Electron) старый UI уже использует Krea2 по умолчанию: кнопки качества ни на что не влияют, при пакете > 1 показывается только первая картинка — до этапа 3.
