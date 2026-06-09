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