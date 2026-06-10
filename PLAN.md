# План: переиспользуемый Telegram-клиент-мессенджер `tg_messenger`

## Context

Пользователь хочет вручную переписываться в личных диалогах Telegram. В проекте уже
есть мощный Telegram-слой (`src/telegram/client_pool.py`, dialogs-команды, web/dialogs),
но он заточен под мониторинг/сбор каналов многими аккаунтами, а не под интерактивный
мессенджер.

`tg_messenger` — **отдельный проект/репозиторий ВНЕ** `tg_content_factory` (создаётся в
`~/Projects/tg_messenger/`, рядом с основным проектом). Со своим `pyproject.toml`, своим
git и своим venv. В `tg_content_factory` подключается **как внешняя зависимость**
(`pip install -e ../tg_messenger`, либо из git-URL). Так выполняются оба требования:
- запускается **автономно** в своём venv (`tg_messenger ...`, без основного проекта);
- **переиспользуется** в `tg_content_factory` импортом `from tg_messenger.core import StandaloneTelegramClient`;
- держит **свои независимые StringSession** (в `~/.tg_messenger/`, не в БД проекта), но
  умеет **опционально принять готовую StringSession-строку** извне (чтобы переиспользовать
  сессию основного проекта без повторного логина).

Решения по уточнениям: движок — **Telethon**; сессии — **свои**; MVP — **диалоги+история,
realtime-приём/отправка, медиа**; три UI — **Web / TUI / CLI**; CLI на **click**.

## Структура проекта (ровно как на скриншоте пользователя)

Создаётся в `~/Projects/tg_messenger/` — отдельно от `tg_content_factory`.

```
tg_messenger/                          # отдельный репозиторий/пакет (~/Projects/tg_messenger/)
├── pyproject.toml                     # deps: telethon, textual, fastapi, uvicorn, click
├── src/
│   └── tg_messenger/
│       ├── __init__.py                # version, публичный API
│       ├── core/                      # ЯДРО — не зависит ни от одного UI
│       │   ├── __init__.py
│       │   ├── client.py              # StandaloneTelegramClient — ядро
│       │   ├── auth.py                # send_code / sign_in / session manage
│       │   ├── models.py              # Pydantic: Dialog, Message, User
│       │   ├── events.py              # EventBus — fan-out входящих к N подписчикам
│       │   └── flood.py               # вендоренный flood-wait-retry (без src.*)
│       ├── cli/
│       │   ├── __init__.py
│       │   └── main.py                # click-группа: login, dialogs, send, read, listen
│       ├── tui/
│       │   ├── __init__.py
│       │   └── app.py                 # Textual App: два списка + ввод
│       └── web/
│           ├── __init__.py
│           ├── app.py                 # FastAPI + SSE + HTML
│           └── templates/
│               └── chat.html          # HTMX + SSE чат-страница
├── tests/                             # TDD: тест пишется ДО кода каждого модуля
│   ├── conftest.py                    # FakeTelethonClient, фикстуры client/tmp-session-dir
│   ├── test_models.py                 # цикл 1
│   ├── test_flood.py                  # цикл 2
│   ├── test_auth.py                   # цикл 3 (session-store + send_code/sign_in)
│   ├── test_events.py                 # цикл 4 (EventBus fan-out)
│   ├── test_client.py                 # цикл 5 (dialogs/history/send/listen)
│   ├── test_cli.py                    # цикл 6 (click CliRunner)
│   ├── test_media.py                  # цикл 7 (send_media/download_media)
│   └── test_web.py                    # цикл 8 (FastAPI TestClient + SSE)
└── README.md
```

`src/`-layout (как на скриншоте) — стандартная упаковка пакета. Поскольку `tg_messenger`
живёт отдельным проектом, верхним пакетом остаётся `tg_messenger` (не `src`), конфликта с
`src/` основного проекта нет.

## Архитектурный принцип: подпакет `core/`

`core/` — это ядро, которое НЕ знает ни про CLI, ни про TUI, ни про Web. Все три UI
импортируют только `from tg_messenger.core import StandaloneTelegramClient`. Внутри ядро
держит один Telethon-клиент и один `events.NewMessage`-хендлер (`core/events.py`), раздающий
входящие N подписчикам через asyncio fan-out. Три UI потребляют ОДИН поток `client.listen()`:
Web — через SSE, TUI — через Textual worker, CLI — через asyncio-таск. Логика не дублируется.
Пакет НЕ импортирует `src.*` — flood-wait (`core/flood.py`) и StringSession-обработка
(`core/auth.py`) вендорятся копией, чтобы автономность была настоящей.

## Содержимое модулей `core/`

- **`core/client.py` — `StandaloneTelegramClient` (ядро).**
  Конструктор `(api_id, api_hash, *, session_name="default", external_session=None)`:
  при `external_session` оборачивает строку через `auth.from_external(...)` (хук
  переиспользования сессии проекта, без записи в файлы), иначе грузит из `~/.tg_messenger/`.
  Методы (маппинг в Pydantic-модели; логику диалогов брать из
  `client_pool.get_dialogs_for_phone`, но только DM, без DB-кэша):
  `connect/disconnect/is_authorized`, `dialogs(dm_only=True)`, `history(peer, limit, offset_id)`,
  `send_text(peer, text)`, `send_media(peer, path, caption)`, `download_media(message, dest)`.
  Регистрирует один хендлер и публикует входящие в `core/events.py`:
  `add_event_handler(..., events.NewMessage(incoming=True))` → `bus.publish(...)`;
  `listen() -> AsyncIterator[IncomingEvent]` отдаёт поток подписчику. Все сетевые вызовы —
  через `core/flood.py`.

- **`core/events.py` — `EventBus`** — `set[asyncio.Queue]`; `publish(event)` (non-blocking,
  drop-oldest, чтобы не блокировать Telethon), `subscribe() -> AsyncIterator[IncomingEvent]`
  с очисткой подписки при отмене. Единый realtime-источник для всех трёх UI.

- **`core/flood.py`** — вендоренный slim из `src/telegram/flood_wait.py` (без pool/DB):
  `run_with_flood_wait_retry`, `HandledFloodWaitError`. ~80 строк.

- **`core/auth.py`** — `send_code(phone)`, `sign_in(code)`, `check_password(pw)` (2FA); стор
  сессий: `load(name)`/`save(name, session)` (файл StringSession 0600 в `~/.tg_messenger/`),
  `from_external(string)` (обернуть без записи). Валидация `auth_key`/`dc_id` по образцу
  `src/telegram/session_materializer.py`.

- **`core/models.py`** — Pydantic v2: `Dialog`, `Message`, `User`, `MediaRef`, `IncomingEvent`.

- **`tg_messenger/__init__.py`** — `__version__` + ре-экспорт публичного API из `core`:
  `StandaloneTelegramClient`, `Dialog`, `Message`, `User`.

## Три интерфейса (поверх `core`, через `client.listen()`)

Каждый UI делает только `from tg_messenger.core import StandaloneTelegramClient` и не лезет
в чужие UI.

- **`cli/main.py` (click)** — `click.group`: `login` (phone→code→2FA → StringSession в
  `~/.tg_messenger/`), `dialogs`, `read <id> --limit N`, `send <id> <text> --file path`,
  `listen` (печать входящих), `chat <id>` (REPL: таск печатает `client.listen()`, основной
  цикл шлёт stdin), `serve`, `tui`. Каждая команда оборачивает `async def` в `asyncio.run`;
  ловит flood-wait. (`__main__.py` не нужен — точка входа через `[project.scripts]`.)
- **`tui/app.py` (Textual)** — два списка + ввод: `ListView` диалогов слева, `MessageBubble`
  справа, `TextArea`+кнопка снизу; переиспользует стек `src/cli/commands/agent_tui.py`
  (`ThreadItem`/`ThreadSelected`, `StreamingMessage`); worker на `client.listen()` обновляет
  чат. CSS инлайном в `app.py` или соседним `.tcss` (по вкусу при реализации).
- **`web/app.py` (FastAPI+HTMX+SSE)** — lifespan стартует клиента в `app.state`; роуты:
  `GET /` (`chat.html`), `GET /dialogs` и `GET /dialogs/{id}/messages` (HTMX-фрагменты),
  `POST /send` (HTML-фрагмент), `POST /dialogs/{id}/media` (`python-multipart`),
  `GET /stream/{id}` как `text/event-stream` поверх `client.listen()` (SSE = разрешённый
  EventSource-случай по политике CLAUDE.md строки 142-145). `templates/chat.html` — HTMX + SSE.

## Интеграция в `tg_content_factory` (как внешняя зависимость)

В `tg_content_factory` добавить зависимость: `pip install -e ../tg_messenger` (для разработки)
или git-URL в `pyproject.toml`. Дальше — обычный импорт:
```python
from tg_messenger.core import StandaloneTelegramClient
client = StandaloneTelegramClient(api_id, api_hash, external_session=existing_string_session)
```
`from_external` оборачивает StringSession-строку без записи в файлы пакета — нулевая завязка
на БД проекта. Опциональный адаптер (достать StringSession из `ClientPool`/БД проекта и собрать
клиента) живёт в `tg_content_factory`, не в пакете, чтобы `tg_messenger` оставался чистым и
автономным.

## Подход: строгий TDD (Red → Green → Refactor)

Каждый модуль рождается из теста. Дисциплина на каждый цикл:
1. **Red** — написать тест на следующее минимальное поведение, запустить `pytest <файл> -x`,
   убедиться что он падает (по правильной причине — `ImportError`/`AssertionError`, не опечатка).
2. **Green** — написать минимум кода, чтобы тест прошёл. Ничего лишнего «на будущее».
3. **Refactor** — почистить код и тест при зелёном баре; `ruff check` чистый, варнингов нет.
Коммит — в конце каждого зелёного цикла (только по явному запросу пользователя).

**Изоляция от сети.** `tests/conftest.py` даёт `FakeTelethonClient` — подменяет
`telethon.TelegramClient`: отдаёт заготовленные dialogs/messages, записывает `send_message`/
`send_file`, и умеет «толкнуть» фейковый `NewMessage`-event в зарегистрированный хендлер.
Все unit-тесты идут без реального Telegram. `tmp_path` под session-dir (не трогаем
`~/.tg_messenger/`). `asyncio_mode="auto"`, `pytest-timeout`.

## Цикл сборки (нулевой → восьмой; каждый = Red→Green→Refactor)

Порядок снизу вверх: сначала то, у чего нет зависимостей, потом ядро, потом UI. Каждый
следующий цикл стартует только на зелёном предыдущем.

- **Цикл 0 — скелет.** Тест: `test_import` (`import tg_messenger`, `__version__` строкой).
  Green: `pyproject.toml`, пустые `__init__.py`, `pip install -e .` в venv. Доказывает автономность.
- **Цикл 1 — `core/models.py`.** `test_models.py`: валидные `Dialog`/`Message`/`User`/`MediaRef`/
  `IncomingEvent`, `out`-флаг, опциональная медиа, отказ на битых данных. → модели.
- **Цикл 2 — `core/flood.py`.** `test_flood.py`: `run_with_flood_wait_retry` ретраит на
  transient FloodWait (фейковая ошибка с `.seconds`), пробрасывает не-flood, отдаёт результат.
- **Цикл 3 — `core/auth.py`.** `test_auth.py`: round-trip `save`/`load` StringSession (файл 0600
  в `tmp_path`), `from_external` оборачивает строку без записи на диск, валидация `auth_key`/`dc_id`;
  `send_code`/`sign_in`/`check_password` на `FakeTelethonClient`.
- **Цикл 4 — `core/events.py`.** `test_events.py`: `EventBus` — один `publish` доходит до всех
  подписчиков; отписка/отмена чистит очередь; переполнение роняет старейшее, не блокируя.
- **Цикл 5 — `core/client.py` (ядро).** `test_client.py` на `FakeTelethonClient`: `dialogs(dm_only)`
  фильтрует только User и маппит в `Dialog`; `history` → list[Message]; `send_text` зовёт
  Telethon и возвращает `Message`; зарегистрированный `NewMessage` → `listen()` отдаёт
  `IncomingEvent`; `external_session=` не пишет файлов.
- **Цикл 6 — `cli/main.py` (click).** `test_cli.py` через `CliRunner` + стаб-клиент:
  `dialogs`/`read`/`send` печатают ожидаемое; `login` сохраняет сессию; flood-wait → дружелюбный
  вывод. (`serve`/`tui` — smoke: команда диспатчится.)
- **Цикл 7 — медиа.** `test_media.py`: `send_media` зовёт `send_file`; `download_media` пишет в
  `dest` под media-dir; CLI `send --file` и download-ветка `read`.
- **Цикл 8 — Web.** `test_web.py` через FastAPI `TestClient` + стаб-клиент в `app.state`:
  `GET /dialogs` и `/dialogs/{id}/messages` отдают HTML-фрагменты; `POST /send` → фрагмент;
  `GET /stream/{id}` отдаёт один SSE-кадр когда `bus.publish`.
- **TUI** проверяется smoke-тестом монтирования (Textual `run_test`/pilot) — без живого Telegram.

## Циклы 9–25 — агентный слой (`tg-messenger agent`, LangGraph + deepagents)

Поверх ядра — авто-ответчик на входящие лички: LangGraph-роутер классифицирует намерение
(**chat** → одиночный вызов модели через `init_chat_model`; **task** → deep-агент
`deepagents.create_deep_agent` с Telegram-инструментами и веб-поиском), ответ уходит в тот же
диалог. Sibling-пакет `agent/` (core его не импортирует); LLM-стек — optional extra `[agent]`;
все модельные контакты инжектируются (юнит-тесты без сети и без реального LLM; на чистом
`.[dev]` агентные тесты скипаются через `importorskip`).

- **Цикл 9 — `agent/config.py`.** `test_agent_config.py` (stdlib-only): `AgentConfig.from_env` —
  `TG_AGENT_MODEL` строго `provider:model`; `TG_AGENT_ALLOWLIST` — `*` = всем, иначе CSV из
  id/@username (нормализация), пустой → ошибка (явный выбор, не тихий дефолт);
  `TG_AGENT_SEARCH` из 4 провайдеров, дефолт duckduckgo. Тут же extra `[agent]` в pyproject +
  smoke `python -W error -c "import langgraph, langchain, deepagents"`.
- **Цикл 10 — `agent/tools.py`.** `test_agent_tools.py` на стаб-клиенте: три async-функции с
  docstring/аннотациями (это схема инструмента для модели) — `send_telegram_message` (зовёт
  `client.send_text`, подтверждает id), `read_telegram_history` (формат `←/→ [id] text`),
  `list_telegram_dialogs`; пустые история/диалоги → внятная строка. Без LangChain-импортов.
- **Цикл 11 — `agent/search.py`.** `test_agent_search.py`: `build_search_fn(provider)` →
  `web_search(query, max_results)`; SDK подменяются фейк-модулями в `sys.modules`; lazy-import
  (отсутствие пакета → ValueError с pip-подсказкой), отсутствие ключа → fail-fast на старте;
  duckduckgo без ключа; brave — REST через httpx.
- **Цикл 12 — `agent/orchestrator.py`.** `test_agent_orchestrator.py`
  (`importorskip("langgraph")`): настоящий граф classify → chat | task с инжектированными
  `classify_fn`/`chat_fn`/`task_agent`; память диалога между ходами (`InMemorySaver`,
  `thread_id` = dialog_id), изоляция диалогов, внутренние сообщения deep-агента не протекают
  в историю (в state — только финальный ответ).
- **Цикл 13 — `agent/runner.py`.** `test_agent_runner.py`: listen → фильтры (out=True; без
  текста; allowlist по id и @username — резолв через `dialogs()` один раз на старте,
  нерезолвленный → warning) → `handle` → `send_text` в тот же диалог; ошибка одного сообщения
  логируется (`logger.exception`) и не роняет цикл; `notify_errors` шлёт короткую заглушку
  (и её падение — тоже только лог). Петля исключена: core подписан `NewMessage(incoming=True)`.
- **Цикл 14 — `agent/factory.py` + CLI.** `test_agent_cli.py`: `build_orchestrator` —
  единственная точка LLM-стека (monkeypatch `factory.init_chat_model` /
  `factory.create_deep_agent`); классификатор парсит односложный ответ, мусор → `chat` +
  warning; команда `agent` по образцу `listen` (шов `make_agent_runner` рядом с `make_client`);
  без extra → ClickException с `pip install "tg-messenger[agent]"`; ошибка конфига → текст
  ValueError без Traceback. Финал: `.env.example` (блок TG_AGENT_*), CLAUDE.md.

- **Цикл 15 — индикатор «печатает…».** `test_client.py`: `client.typing(peer)` — безопасный CM
  поверх Telethon `action(peer, "typing")` (`_SafeChatAction`): шлёт действие периодически,
  гасит на выходе; **по контракту не бросает** — сбой входа/выхода = warning в лог, тело и
  исключения тела проходят нормально. `test_agent_runner.py`: индикатор активен, пока `handle`
  думает, и гаснет после ответа — runner просто `async with client.typing(...)`, без обёрток.

- **Цикл 16 — фиксы по ревью.** `test_agent_config.py`: `'*'` вперемешку с другими записями →
  ValueError (раньше — молчаливая блокировка всех). `test_agent_runner.py`: инвариант «фильтр
  по sender_id» зафиксирован тестом с расходящимися dialog_id/sender_id. `test_agent_search.py`:
  ленивый генератор ddgs исчерпывается внутри worker-потока (`list()` в `to_thread`), не в
  event loop; фейк brave-клиента стал одноразовым (нет утечки состояния между тестами).
  Refactor: ответ графа — в выделенном ключе `reply` состояния, не `messages[-1]`.

- **Цикл 17 — трассировка LangSmith.** Кода в графе нет: langchain/langgraph трассируются
  сами по env `LANGSMITH_TRACING`/`_API_KEY`/`_PROJECT` (`langsmith` уже в зависимостях
  langchain), `thread_id` группирует трейсы по диалогам. `test_agent_config.py`:
  `langsmith_tracing_enabled` — off по умолчанию, on с ключом, on без ключа → ValueError
  (иначе фоновые ошибки на каждый трейс). `test_agent_cli.py`: команда `agent` печатает
  `LangSmith tracing: on (project=...)` при включённой трассировке, падает с подсказкой
  про `LANGSMITH_API_KEY` без ключа, молчит при выключенной.

- **Циклы 18–25 — конфигурируемые интенты: vision, голосовые, agent.json.**
  - **18 — core: честные медиа-типы.** `MediaKind` + `"voice"` (Telethon: `.voice` проверяется
    ДО `.document` — голосовое И есть document), `MediaRef.mime_type` из `file.mime_type`.
  - **19 — `agent/media.py`** (stdlib+core): `download_image(client, dialog_id, message)` →
    `ImageInput(base64_data, mime_type)`; `MAX_IMAGE_BYTES` проверяется по заявленному size
    ДО скачивания и по реальным байтам после; tmp-каталог чистится даже при ошибке;
    mime-фолбэк `image/jpeg`.
  - **20 — runner: диспетчеризация.** Порядок: out → allowlist (до любого скачивания) →
    voice (`logger.info` + skip) → photo (download → `handle(..., image=)`, подпись =
    `message.text`) → no-text skip → текст. Ошибка скачивания — `logger.exception`, цикл жив.
  - **21 — orchestrator: vision-узел.** Условный вход `START → vision | classify` по
    `has_image` (handle пишет его явно на каждом ходе). Картинка НЕ в state: мультимодальное
    сообщение уходит в `vision_fn` через `_pending_images` (валидно при последовательном
    runner'е), в checkpointed-истории — текстовый плейсхолдер `[изображение] <подпись>` +
    ответ — следующий текстовый ход не-vision модели не ломается о base64.
  - **22 — factory: vision-модель.** `TG_AGENT_VISION_MODEL` (provider:model), фолбэк на
    основную (тогда она должна быть мультимодальной); `make_vision_fn` с `VISION_SYSTEM_PROMPT`.
  - **23 — config: agent.json.** `load_intents` → `IntentSpec(name, description, pipeline,
    system_prompt?)`; путь: `TG_AGENT_CONFIG` (явный — обязан существовать) или `./agent.json`;
    валидация fail-fast: имя — одно слово не из `RESERVED_INTENT_NAMES`, pipeline ∈ {chat, task},
    description непустой, неизвестные ключи — ошибка; все ValueError с путём файла.
  - **24 — orchestrator: кастомные узлы.** Узел на интент строится из конфига при сборке графа;
    `system_prompt` интента префиксуется к user-сообщению ТОЛЬКО в payload вызова
    (`_prefix_instruction`) — история хранит оригинал; неизвестный интент от классификатора
    клампится в `chat` с warning (нет KeyError в conditional edge).
  - **25 — factory+CLI.** Промпт классификатора генерируется из списка интентов
    (`build_classify_prompt`); `make_agent_runner` печатает vision-модель и имена
    загруженных интентов; `agent.json.example` — образец конфига.

Принятые компромиссы v1: полное доверие allowlist'у — разрешённый собеседник может через
задание читать/отправлять в любые диалоги (зафиксировано в `.env.example`); последовательная обработка (долгий task может вытеснить старые
события из очереди EventBus — drop-oldest уже логируется); история в `InMemorySaver` живёт до
рестарта процесса и не ограничена; @username-allowlist матчится только по существующим
диалогам (надёжнее числовой id); инструкция кастомного интента уходит user-текстом, не
системной ролью (SystemMessage посреди списка ломается у части провайдеров); картинка-как-
документ (без сжатия) не обрабатывается — только telegram-«photo»; голосовые определяются,
но не обрабатываются (v1).

## Циклы 26–30 — отслеживание удалений своих сообщений (`tg-messenger watch`)

Боты-модераторы в группах удаляют сообщения пользователя (неотвеченная капча и т.п.) —
написанное теряется. v1: бэкап удалённых в Saved Messages; авто-капча — не в этом релизе.

- **Цикл 26 — модели.** `OutgoingEvent(dialog_id, message)`, `MessagesDeletedEvent(chat_id |
  None, message_ids)` — Telegram называет чат только для каналов/супергрупп.
- **Цикл 27 — поток своих сообщений.** conftest: `push_event` диспатчит по типу builder
  (как настоящий Telethon) — refactor на зелёном. `listen_outgoing()` через отдельный
  EventBus (`Generic[T]`), handler `NewMessage(outgoing=True)` БЕЗ is_private-фильтра
  (группы — суть фичи); входящий DM-поток не тронут. `get_me() -> User`.
- **Цикл 28 — поток удалений.** `listen_deleted()`; `entity_title(peer)` (фикс
  `_entity_title`: приоритет `.title` — названия групп игнорировались).
- **Цикл 29 — `core/watch.py` `DeletionWatcher`.** Кэш своих сообщений (OrderedDict,
  1000, eviction) — единственный фильтр «своих»: `deleted_ids` не несут автора. Матч
  изымает запись (повтор события не дублирует уведомление); событие без chat_id не
  матчит канальные записи (`CHANNEL_ID_THRESHOLD` — пер-канальные id пересекаются с
  глобальными); self-диалог не кэшируется (нет цикла уведомлений); одно уведомление
  на (событие × диалог); title best-effort с фолбэком на id; сбой send_text — в лог,
  цикл живёт. `run()` — `asyncio.gather`, не TaskGroup (KeyboardInterrupt завернулся
  бы в BaseExceptionGroup и сломал Ctrl+C в CLI).
- **Цикл 30 — CLI `watch`.** Демон по паттерну `listen`; `echo=click.echo` — summary
  в консоль.

Принятые компромиссы v1: `MessageDeleted` не гарантирован Telegram'ом (пропуски
возможны — best-effort); кэш в памяти (рестарт = потеря, работает только пока `watch`
запущен); собственные удаления тоже дают уведомление (актора в событии нет); правки
(`MessageEdited`) не трекаются; авто-нажатие капчи — следующий релиз.

## Циклы 31–38 — вкладки «DM / Группы» (web, TUI, CLI)

Вторая вкладка рядом с DM во всех трёх интерфейсах: всё не-DM — группы, супергруппы,
каналы-бродкасты, боты. Живые обновления в открытой группе — полноценные, как в DM.
Агент намеренно остаётся DM-only.

- **Цикл 31 — `Dialog.kind` + `_dialog_kind`.** `DialogKind = dm|group|channel|bot`
  (default "dm" — стабы не ломаются). Классификатор: bot → "bot"; title → "channel"
  при broadcast=True, иначе "group" (Chat без атрибута broadcast — тоже group);
  имена → "dm"; неизвестная сущность → "group" (fail-safe вне DM). `_is_dm_entity`
  переписан через kind — поведение бит-в-бит. conftest: `FakeChannel(broadcast=)`,
  новый `FakeChat` (title, БЕЗ broadcast — как настоящий telethon Chat).
- **Цикл 32 — `dialogs(dm_only=False)`: kind + marked id.** Критичный фикс: раньше
  `Dialog.id = entity.id` (голый, положительный), а события несут marked id
  (отрицательный для групп/каналов) — фильтры live-потоков для групп не совпали бы
  никогда. Теперь `id=int(getattr(d, "id", entity.id))` — телетоновский `Dialog.id`
  уже marked. conftest `FakeDialog` мимикрирует (`_marked_id`: Channel → `-100…`,
  Chat → `-id`).
- **Цикл 33 — `listen_all()`.** Третья шина входящих: `_on_new_message` маппит один
  раз и публикует в `_bus_all` всегда, в `_bus` — только private. `listen()` не
  изменился ни на бит (агент зависит от него). Никакого merge генераторов в UI —
  это create_task/cancel и «Task was destroyed»-варнинг при filterwarnings=error.
  Проверка is_private перенесена внутрь try: битое групповое событие логируется.
- **Цикл 34 — web-вкладки.** `GET /dialogs?tab=dm|groups` (неизвестный tab → dm,
  без 400); кнопки в `chat.html` (`hx-get` → `#dialogs ul`), active-класс — 3 строки
  delegated-JS; `_dialog_li` несёт `data-kind`. Существующий JS выбора диалога
  парсит и отрицательные id.
- **Цикл 35 — web SSE → `listen_all()`.** Одна строка в `sse_event_stream` — фильтр
  по dialog_id корректен для любого вида без эвристик по знаку id.
- **Цикл 36 — TUI-вкладки.** `Tabs(Tab DM, Tab Группы)` над единственным ListView
  в `Vertical#sidebar` (НЕ TabbedContent — ре-парентит ListView). Гард
  `self._started` гасит TabActivated при mount (клиента ещё нет); имя НЕ `_ready` —
  у Textual `App._ready` уже есть (коллизия даёт «'bool' is not callable»).
  Перезагрузка списка — только `run_worker(group="dialogs", exclusive=True)`.
- **Цикл 37 — TUI live из групп.** `_drain_incoming` → `listen_all()`.
- **Цикл 38 — CLI `dialogs --groups`.** Не-DM списком с пометкой `[kind]`
  (виды смешаны в одной выдаче).

Принятые компромиссы v1: бродкаст-каналы read-only — отправка без прав даёт ошибку
Telegram (web 500-фрагмент / TUI notify / CLI ClickException), превентивной блокировки
composer нет; агент и CLI `listen`/`chat` остаются DM-only; `Dialog.id` для
групп/каналов сменился на marked id (согласован с `event.chat_id`, пригоден для
history/send); unread-бейджи не live; SSE — один диалог на стрим.

## Финальная верификация (после зелёных циклов)

- **Вся сюита**: `pytest -q` зелёная, `ruff check src/ tests/` чистый, варнингов нет.
- **Автономно**: свежий venv `pip install -e ./tg_messenger`; `tg_messenger --help`;
  `tg_messenger login` создаёт сессию в `~/.tg_messenger/`; `dialogs`/`chat`/`serve`/`tui`
  против тестового аккаунта — доказывает работу без основного проекта.
- **Как внешняя зависимость**: в venv `tg_content_factory` сделать `pip install -e ../tg_messenger`,
  затем `python -c "from tg_messenger.core import StandaloneTelegramClient"`; интеграционный
  тест: прокинуть реальную StringSession проекта через `external_session=` → `dialogs()`
  отдаёт DM, файлов пакета не создаётся.
- Живые Telegram-тесты — только с явного согласия (правило проекта).
```