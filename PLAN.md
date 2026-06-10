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

## Циклы 39–46 — анти-флуд: TTL-кэш dialogs/history + единый контур FloodWait

Инцидент на живом тесте PR #7: быстрое переключение вкладок DM/Группы → каждый
клик = `dialogs()` = `GetDialogsRequest` → FloodWait 22s. Telethon с дефолтным
`flood_sleep_threshold=60` молча спал до 60s (UI висел «loading»), а наш
`run_with_flood_wait_retry` этих флудов не видел — Telethon кидает `FloodWaitError`
только при wait > threshold. Фикс — в `core` (правильная высота: все три UI
получают его бесплатно).

- **Цикл 39 — `TTLCache` get/set/TTL/eviction** (`core/cache.py`, stdlib-only).
  `OrderedDict[key, (value, expires_at)]`; `clock` инжектится (`time.monotonic` по
  умолчанию) — тесты двигают фейк-часы, не спят. Живо при `clock() < stored_at+ttl`,
  мертво при `>=`; `maxsize`-eviction старейших (паттерн `watch.py`); повторный
  `set` обновляет expiry и порядок.
- **Цикл 40 — `invalidate`/`invalidate_if`**: точечно; `None` → всё; missing →
  no-op; `invalidate_if(pred)` собирает ключи и удаляет (не мутирует dict в итерации).
- **Цикл 41 — single-flight `get_or_fetch`**: per-key `asyncio.Lock`, double-check
  под локом → конкурентные миссы одного ключа коалесцируются в один fetch; упавший
  fetch НЕ кэшируется и не деадлочит (`try/finally`, лок снимается).
- **Цикл 42 — кэш `dialogs()`** (фикс инцидента). Кэшируется ОДИН полный смаппленный
  список (все kind, `maxsize=1`); `dm_only=True` фильтрует `kind=="dm"` из кэша →
  dm→groups→dm = 1 сетевой вызов. TTL 30s. Возврат — копия (защита кэша).
- **Цикл 43 — кэш `history()`**: ключ `(int(peer), limit, offset_id)`, TTL 15s,
  `maxsize=64`. Возврат — копия; пагинация по `offset_id` — точный ключ, страницы
  не склеиваются.
- **Цикл 44 — инвалидация `history`**: `_invalidate_history(peer)` =
  `invalidate_if(k[0]==int(peer))`. Зовётся в `send_text`/`send_media` (свежее
  сообщение видно при переоткрытии), в `_on_new_message`/`_on_outgoing_message`
  (внутри `try`, сразу после dialog_id, ДО маппинга — битое событие тоже
  инвалидирует), в `_on_deleted` (по `chat_id` если есть, иначе полный сброс
  history — удаления редки).
- **Цикл 45 — `flood_sleep_threshold=0`** в `_default_factory`: Telethon больше
  никогда не спит молча, все FloodWait идут через `run_with_flood_wait_retry`
  (≤60s ретрай в бюджете 120s, иначе `HandledFloodWaitError` → web 503 / CLI
  ClickException / TUI notify).
- **Цикл 46 — финал**: полный `pytest` + `ruff`, PLAN.md/CLAUDE.md, push в
  `feat/dialog-tabs` (PR #7).

Решённые вопросы / компромиссы v1: `fresh=True`-байпас НЕ делаем (приглашение
воссоздать флуд); dialogs-кэш событиями НЕ инвалидируется — намеренно (активная
группа свела бы кэш на нет, вернулся бы инцидент; staleness ≤30s списка и ≤15s
неоткрывавшейся истории принят, собственные действия и live-события инвалидируют
history мгновенно); копия shallow (список копируется, модели шарятся — конвенция
«UIs render, never mutate»); кэш в памяти процесса; `download_message_media` НЕ
кэшируется (точечный fetch по id); `entity_title` не трогаем (watch мемоизирует
titles сам). Дисциплина username-резолва: дорогой (~50 подряд → флуд), никогда
`get_entity('@username')` в циклах — allowlist резолвится через dialog list однажды.

## Циклы 47–50 — импортируемость как библиотека (#9, сделано)

Цель umbrella #6: `pip install tg-messenger` = core+CLI, web/tui — extras, сторонний
проект импортирует ядро без лишнего стека. `tests/test_packaging.py`:
- **47** — изоляция core от UI-стека: fresh-subprocess проверяет, что `import tg_messenger`
  и `import tg_messenger.cli.main` не тянут fastapi/uvicorn/textual/jinja2 в `sys.modules`.
- **48** — понятные ошибки без extra: `serve`/`tui` оборачивают ленивый импорт в try/except,
  на `ImportError` → `ClickException("... pip install 'tg-messenger[web]'/[tui]")`.
- **49** — публичный API: снапшот `tg_messenger.__all__` и `tg_messenger.core.__all__`;
  добавлены реэкспорты `SessionStore`, `LoginFlow`, `LOGIN_HINT`, `EventBus`,
  `run_with_flood_wait_retry`, `HandledFloodWaitError`.
- **50** — pyproject: base = telethon>=1.43/pydantic/click; extras `[web]`/`[tui]`/`[all]`;
  `[dev]` тянет `[web,tui]`; `src/tg_messenger/py.typed` в wheel; README «Use as a library».

## Циклы 51–56 — шифрование сессий (Fernet) + SSO с фабрикой (#10, сделано)

`core/session_cipher.py` + `SessionStore(encryption_key=)`:
- **51** (`test_session_cipher.py`): `encrypt_session`/`decrypt_session`/`is_encrypted`;
  `enc:v2:` = Fernet над PBKDF2(secret, salt=`b"tg_session_key_v2"`, 200k, 32) — схема
  фабрики байт-в-байт (тест переderives ключ с захардкоженными константами независимо);
  v1 read-only, plaintext passthrough, неверный ключ → ValueError.
- **52**: monkeypatch ImportError `cryptography` → при ключе понятная ошибка с `[crypto]`.
- **53/54** (`test_auth.py`): save пишет `enc:v2:` (без plaintext-подстроки), load расшифровывает,
  чтение чужого `enc:v2:` с тем же ключом (SSO); ленивая миграция plaintext→enc (0600 сохранён);
  enc-файл без ключа → ошибка с подсказкой про `SESSION_ENCRYPTION_KEY`.
- **55** (`test_cli.py`): `login --export-session` печатает строку + warning (не в лог);
  `--import-session` валидирует и сохраняет, мусор → «invalid StringSession».
- **56**: pyproject `[crypto]` (в `[all]`/`[dev]`); `.env.example` (`SESSION_ENCRYPTION_KEY`);
  README «Session encryption & SSO»; CLAUDE.md/PLAN.md.

## Циклы 57–61 — мультилогин / профили (#11, сделано)

`--profile` сквозной во всех интерфейсах; профиль = сохранённый session-файл.
- **57** (`test_auth.py`): `SessionStore.list_profiles()` — отсортированные имена `*.session`
  без расширения; нет каталога → `[]`.
- **58** (`test_cli.py`): глобальная опция `--profile` → `session_name`; команда `profiles`;
  helpers `_session_store()`/`_is_interactive()`/`_resolve_profile()`/`_effective_session()`;
  `_with_client` резолвит профиль через `click.get_current_context(silent=True)`;
  `login` чтит глобальный `--profile`, но без меню (создаёт/заменяет профиль).
- **59** (`test_cli.py`): >1 профиля без флага → интерактивное меню (выбор по номеру);
  неинтерактивный stdin → ошибка `pass --profile NAME`; 0/1 профиля резолвится молча.
- **60** (`test_tui.py`): `ProfileItem`, `ProfileScreen(ModalScreen[str])`,
  `MessengerTUI(profiles=, client_factory=)` — стартовый экран выбора при >1 профиле.
- **61** (финал):
  - **изоляция логов** (`test_logsetup.py`): `setup_logging(profile=)`/`log_file_path(profile=)` →
    `tg_messenger_<profile>.log` для не-default (default = общий файл); CLI прокидывает
    глобальный `--profile`.
  - **web `/profiles`** (`test_web.py`): read-only список профилей + пометка активного
    (`session_name` из `build_app`); каталог сессий через `TG_SESSION_DIR`.
  - **serve/tui `--profile`** (`test_cli.py`): оба чтут глобальный `--profile` как `session_name`
    (через `_effective_session`).
  - доки: CLAUDE.md (Interfaces), README («Multiple accounts (profiles)»), PLAN.md.

## Циклы 62–66 — поиск диалогов и сообщений (#12, сделано)

- **62** (ядро, `core/search.py`): `filter_dialogs(dialogs, query)` — чистая, без сети,
  поверх уже загруженного списка (#8-кэш). Title-подстрока (ci), username с/без `@`
  (exact/prefix), id (точный + positive form marked id), пустой query → весь список.
  Тесты `test_search.py`.
- **64** (ядро, `client.search_messages`): `search_messages(peer, query, limit=20)` =
  `iter_messages(search=query)` через `run_with_flood_wait_retry`, БЕЗ кэша (точечный
  lookup, не страница). Глобального поиска по всем чатам намеренно нет — это работа
  tg_content_factory. Тесты `test_client.py`.
- **63** (id в UI + регрессы): `_dialog_li` (web), `DialogItem` (TUI), команда `dialogs`
  (CLI) показывают id рядом с заголовком; регресс-тесты во всех трёх (`test_web.py`/
  `test_tui.py`/`test_cli.py`).
- **65** (UI поиска):
  - **web** (`test_web.py`): `GET /dialogs?tab=&q=` фильтрует через `filter_dialogs`
    (оба таба); `<input name="q">` над списком (HTMX `hx-get`, скрытый `#current-tab`
    держит таб); `GET /dialogs/{id}/search?q=` → фрагмент найденных сообщений
    (`client.search_messages`).
  - **CLI** (`test_cli.py`): `dialogs --find QUERY` (локальный фильтр, совместим с
    `--groups`); новая команда `search PEER QUERY [--limit]` (печатает через
    `message_line`, как `read`).
- **66** (TUI + доки):
  - **TUI** (`test_tui.py`): `Input#search` над вкладками; `on_input_changed` фильтрует
    видимые `DialogItem` через `filter_dialogs` поверх `self._all_dialogs` (локально,
    без сети). Поиск сообщений внутри диалога в TUI отложен как v1-компромисс.
  - доки: CLAUDE.md (core + Interfaces), README («Search»), PLAN.md.

## Циклы 67–70 — SQLite storage layer (#13, сделано)

`core/storage.py` — `Storage`: фундамент персистентности для #16/#17/#19 (правила
модератора, стилевые профили суфлёра, расписание хартбита, журналы). Кэш сюда НЕ
переезжает (in-memory из #8). `tests/test_storage.py`:
- **67**: connect/close создаёт файл; kv-roundtrip (str/dict/list); get отсутствующего→None;
  context manager закрывает и данные персистятся.
- **68**: миграции через `PRAGMA user_version` (растёт по числу зарегистрированных);
  повторный connect не перенакатывает; две пачки от разных потребителей по порядку;
  ошибка в миграции → rollback всего батча, version не растёт.
- **69**: `asyncio.gather` из 20 set/get без потерь (один conn + `asyncio.Lock` +
  `asyncio.to_thread`, `check_same_thread=False`); execute/fetchone/fetchall параметризованы.
- **70**: `default_db_path(profile)` = `~/.tg_messenger/<profile>.db`; CLAUDE.md/PLAN.md.

## Циклы 71–76 — event-потоки (#14, сделано)

Расширение событийного слоя `core/`: chat-actions, read-receipts, реакции, album_id.
Новые модели в `models.py`, новые ленивые шины/стримы в `client.py` (один паттерн с
`listen_deleted`), фейки в `conftest` диспатчат новые типы событий по атрибутам-маркерам.
`tests/test_models.py` + `tests/test_client.py`:
- **71**: модели `ChatActionEvent` (`dialog_id`, `kind: join|leave|kick|title|pin|photo|other`,
  `user`/`actor`/`raw_text`), `MessageReadEvent` (`dialog_id`/`max_id`/`outbox`),
  `ReactionEvent` (`dialog_id`/`message_id`/`emoticon`/`actor_id`); `IncomingEvent.album_id`.
- **72**: шина `_bus_chat_actions` + `_on_chat_action` (маппит `events.ChatAction`: флаги
  `user_joined`/`user_added`→`join`, `user_kicked`→`kick`, `user_left`→`leave`,
  `new_title`/`new_pin`/`new_photo`, иначе `other`; `user` и `added_by`/`kicked_by`
  best-effort) + `listen_chat_actions()`; регистрация eager в `connect()`. Битое событие →
  `logger.exception`, поток жив; publish без подписчиков = no-op.
- **73**: шина `_bus_reads` + `_on_message_read` (`events.MessageRead`: `dialog_id` из
  `chat_id`, `max_id`, `outbox`) + `listen_reads()`. Тесты: inbox vs outbox.
- **74**: шина `_bus_reactions` + `_on_reaction` через `events.Raw(UpdateMessageReactions)`
  (реальный тип апдейта для user-аккаунтов; `dialog_id` через `telethon.utils.get_peer_id`,
  `message_id=msg_id`, `emoticon` из первого `ReactionEmoji`, иначе `None`; `actor_id=None`
  best-effort) + `listen_reactions()`. Неизвестная структура → `logger.warning` + пропуск.
- **75**: `IncomingEvent.album_id = message.grouped_id` в `_on_new_message`; `send_reaction`
  (`SendReactionRequest`+`ReactionEmoji` через `run_with_flood_wait_retry`). Тесты: album_id
  прокинут; запрос записан; non-transient flood → `HandledFloodWaitError`.
- **76**: CLAUDE.md (семь стримов, album_id, send_reaction) + PLAN.md этот блок.

v1-компромиссы (в рамках плана): альбомы только маркируются `album_id` — агрегатора нет,
потребитель группирует сам; кастомные/премиум-реакции → `emoticon=None`; `actor_id` реакции —
best-effort `None` (raw-апдейт не несёт надёжного единственного автора).

## Циклы 77–82 — базовые действия (#15, сделано)

Действия над сообщениями в `core/` + проброс в UI. Новые поля моделей: `Message.reply_to_id`,
`Dialog.unread` (из `dialog.unread_count`). Фейк `conftest` расширен (`forward_messages`/
`edit_message`/`delete_messages`/`send_read_acknowledge`, `send_message` принимает `reply_to`,
`FakeMessage.reply_to`). Тесты в `test_client.py`/`test_cli.py`/`test_web.py`/`test_tui.py`.
- **77**: `send_text(peer, text, reply_to=None)` прокидывает `reply_to` в telethon `send_message`;
  `_to_message` маппит `reply_to_id` из `raw.reply_to.reply_to_msg_id` (best-effort getattr).
  Инвалидация history своего peer (как раньше).
- **78**: `forward(from_peer, ids, to_peer)` → `forward_messages`, инвалидирует history ОБОИХ
  peer'ов; `edit_text(peer, id, text)` → `edit_message`; `delete_messages(peer, ids, revoke=True)`
  → `delete_messages`. Все через `run_with_flood_wait_retry`; инвалидируют history. Тесты:
  правильные вызовы фейка, инвалидация, non-transient flood → `HandledFloodWaitError`.
- **79**: `mark_read(peer)` → `send_read_acknowledge` (retry, НЕ инвалидирует history);
  регресс на `Dialog.unread` из telethon-диалога.
- **80**: CLI — `send --reply-to`, команды `forward FROM IDS TO`, `edit PEER ID TEXT`,
  `delete PEER IDS [--for-me]` (revoke=False), `mark-read PEER`. IDS через запятую (`_parse_ids`,
  битый токен → `ClickException`). `read` остаётся печатью истории — отметка прочитанным вынесена
  в отдельную команду `mark-read`, чтобы не ломать существующую.
- **81**: web — бейдж `<span class="unread">N</span>` в `_dialog_li`; открытие диалога
  (`GET .../messages`) зовёт `mark_read` best-effort (ошибка → `logger.warning`, история всё равно
  отдаётся); `/send` принимает `reply_to` (Form) и прокидывает в `send_text`. TUI — `(N)` в
  `DialogItem`; `on_list_view_selected` запускает `_mark_read` воркером (best-effort, без await
  в хендлере).
- **82**: CLAUDE.md (блок про действия в client.py) + PLAN.md этот блок.

v1-компромиссы (в рамках плана): в TUI нет выбора сообщения, поэтому reply/forward/edit/delete
из интерфейса не делаются — только авто-`mark_read` при открытии и бейджи непрочитанного; сами
действия доступны через CLI. `unread` — снапшот из `dialogs()` (без live-обновления бейджей).

## Циклы 83–90 — режим модератора (#16, сделано)

Сервис `core/moderation.py` поверх клиента (паттерн `watch.py`): слушает `listen_all()` +
`listen_chat_actions()`, применяет первое сматчившееся правило на чат, журналирует решения в
SQLite. Деструктивен по природе → **dry-run по умолчанию** (`--enforce` — боевой).

- **83**: Pydantic-модели + матчинг — `RuleConditions`/`RuleActions`/`ModerationRule`,
  `rule_matches(...)` с AND-семантикой; regex валидируется fail-fast.
- **84**: хранение правил поверх `Storage` — миграции `moderation_rules` (PK `chat_id,name`)
  и `moderation_log`, `add_rule`/`list_rules`/`remove_rule` (conditions/actions как JSON).
- **85**: `ModerationEngine` — кэш новичков (`OrderedDict`, join-время по `ChatActionEvent`)
  и скользящее 60с-окно частоты на `(chat_id, sender_id)`; `clock` инжектится, без реального
  sleep.
- **86**: dry-run — при `enforce=False` клиент НЕ зовётся, `logger.info("would …")`,
  запись в журнал `dry_run=1`.
- **87**: enforce — delete/mute/ban/warn зовут методы клиента; ошибка одного действия
  `logger.exception` и движок жив; журнал `dry_run=0`; первое сматчившееся правило выигрывает.
- **88**: тонкие обёртки в `client.py` — `mute_user`/`ban_user` поверх `edit_permissions`
  (через `run_with_flood_wait_retry`), плюс `is_admin(peer)` поверх `get_permissions`
  (best-effort → `False`).
- **89**: CLI — `moderate [--enforce]` (паттерн `watch`, Ctrl+C чисто; на старте
  `check_admin_rights` → чаты без прав отключаются предупреждением) и группа
  `moderate-rules list/add/remove` (`add` читает JSON-файл; отрицательный chat_id — через `--`).
  `moderation.json.example` в корне.
- **90**: CLAUDE.md (core: `moderation.py` + обёртки клиента) + PLAN.md этот блок.

v1-компромиссы: `is_forward` всегда `False` (флаг события пока не прокинут в
`process_message`); журнал/правила — per-profile SQLite; admin-проверка best-effort
(чат без прав молча пропускается в рантайме, не валит сервис).

## Циклы 91–98 — суфлёр (#17, сделано)

`agent/suggest.py` — черновик ответа в стиле прошлых переписок для ЧЕЛОВЕКА (не автоответ;
полная автоматизация — отдельное #18). Агент-слой: LLM ТОЛЬКО через инъекцию (`suggest_fn`),
сам `suggest.py` langchain НЕ импортирует — поэтому Suggester/профиль/storage зелёные на голом
`[dev]` (без `importorskip`). `factory.make_suggest_fn`/`build_suggester` — единственное место
с `init_chat_model`.

- **91**: `Suggester(client, suggest_fn, storage=None, history_limit=30)` — `suggest(dialog_id)`
  собирает `history` хронологически, размечает свой/чужой (`out`), грузит профиль если есть
  storage, зовёт `suggest_fn(context, profile)` → текст.
- **92**: `build_style_profile(messages) -> StyleProfile` (чистая) — агрегаты avg_length /
  emoji_freq / greetings / signatures + до 10 примеров (свой ответ сразу ПОСЛЕ входящего);
  пустая история → заглушка нулями.
- **93**: хранение — таблица `style_profiles` (PK `dialog_id`, JSON), `register_suggest_migrations`,
  `save_style_profile`/`load_style_profile` (roundtrip, перезапись).
- **94**: деградация — профиль None → suggest работает; `suggest_fn` бросает → `logger.exception`
  и наружу (UI показывают ошибку, не падают молча).
- **95**: CLI — `make_suggester` seam (через `factory.build_suggester`, требует `TG_AGENT_MODEL`,
  fail-fast `ClickException`); `suggest PEER` печатает черновик, `--send` шлёт, `--learn` строит
  и сохраняет профиль (один history-проход, per-peer, не фон).
- **96**: TUI — входящее в открытом DM → worker зовёт `suggester.suggest`, подсказка в
  `#suggestion` Static (Tab принимает в композер; смягчение — только при пустом композере;
  ввод сбрасывает); `suggester=None` → фича выключена, не падает; сеть/LLM только в `run_worker`.
- **97**: web — `build_app(suggester=)`, `GET /dialogs/{id}/suggest` → текст черновика (503 если
  не сконфигурирован), кнопка 💡 Suggest у композера (`chat.html`, fetch → вставка в input).
- **98**: фиксация last_read — `record_last_read`/`watch_read_receipts` пишут outbox-квитанции
  (`listen_reads`, `outbox=True`) в kv (v1 только пишет, не действует). `.env.example`
  (`TG_SUGGEST_HISTORY=30` + приватность), README (абзац суфлёра + приватность),
  CLAUDE.md (agent: `suggest.py`), PLAN.md этот блок.

v1-компромиссы: суфлёр сам не действует (только черновик; квитанции записаны, но не используются);
`build_style_profile` — лёгкие эвристики (greetings/signatures по словарю, emoji по диапазонам
codepoint, без grapheme-точности); профили/last_read — per-profile SQLite; история уходит в LLM
целиком (приватность задокументирована).

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