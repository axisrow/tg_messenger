# Ghostwrite (этап 2 суфлёра, #18) — детальный дизайн

Детализация ишью-скелета #18 (требование: «дизайн первым коммитом ветки после #17»).
Опираемся на живой опыт #17 (Suggester) и #16 (dry-run/предохранители).

## Что это

`tg-messenger ghostwrite` — сервис, который САМ отвечает в ЯВНО включённых DM в стиле
владельца (через `Suggester` из #17). В отличие от `tg-messenger agent` (универсальный
авто-ответчик с интентами), ghostwrite узко-целевой: «общайся как я в этих диалогах».
Деструктивно (шлёт от вашего имени) → жёсткие предохранители, dry-run по умолчанию.

## Архитектура (паттерн ModerationEngine #16 / watch.py)

`agent/ghostwrite.py` — `GhostwriteEngine(client, suggester, storage, *, enforce=False, clock=, max_per_hour=, pause_on_human_sec=)`:
- `run()` через `asyncio.gather` (НЕ TaskGroup — Ctrl+C), два консьюмера:
  - `listen()` (входящие DM) → на сообщение от включённого контакта: предохранители →
    `suggester.suggest(dialog_id)` → `client.send_text` (enforce) / would-лог (dry-run) → журнал.
  - `listen_outgoing()` → «человек вмешался»: исходящее в ghostwrite-диалоге, которое
    НЕ отправил движок (по кэшу недавних own-id, паттерн watch) → авто-пауза диалога
    на `pause_on_human_sec` (сутки по умолчанию).
- НЕ импортирует LLM-стек (Suggester инжектируется, как и suggest_fn). LLM только в factory.py.

## Предохранители (безопасность — ядро ишью)

1. **Per-dialog allowlist** в storage (таблица `ghostwrite_dialogs`: dialog_id PK, enabled,
   paused_until). Отдельный от `TG_AGENT_ALLOWLIST`. `*` ЗАПРЕЩЁН by design (нет «всем»).
2. **Лимит сообщений/час на диалог** (`max_per_hour`, скользящее окно в памяти, инжектируемый clock).
   Превышен → пропуск + warning, не шлём.
3. **`pause all`** — команда, ставит paused_until = far future всем включённым (kill switch).
4. **Авто-пауза при вмешательстве человека** — см. listen_outgoing выше.
5. **dry-run по умолчанию** (`enforce=False`): would-лог вместо отправки.
6. Журнал всех авто-ответов в storage (`ghostwrite_log`: dialog_id, message_id, reply, dry_run, ts).

## CLI

- `tg-messenger ghostwrite [--enforce]` — запуск движка (паттерн watch: setup, Ctrl+C чисто).
- `tg-messenger ghostwrite-dialogs enable PEER` / `disable PEER` / `list` / `pause-all` / `resume PEER`.
- На старте — список включённых диалогов в лог.

## TDD-циклы (Red → Green)

- **A — storage слой** (`tests/test_ghostwrite.py`): таблицы `ghostwrite_dialogs`/`ghostwrite_log`,
  `register_ghostwrite_migrations`; enable/disable/list_enabled/pause/resume/pause_all/is_active CRUD на tmp.
- **B — движок: dry-run** (enforce=False): включённый диалог, входящее → suggester зовётся,
  client.send_text НЕ зовётся, would-лог, журнал dry_run=1. Выключенный диалог → суфлёр НЕ зовётся.
- **C — движок: enforce + лимит/час**: enforce=True → send_text зовётся с текстом суфлёра, журнал dry_run=0;
  превышение max_per_hour → пропуск + warning (clock инжектится, без sleep). Не-allowlist sender → пропуск.
- **D — авто-пауза при человеке**: listen_outgoing исходящее, НЕ от движка → диалог paused_until растёт;
  отправленное движком own-сообщение паузу НЕ ставит (по кэшу own-id, паттерн watch).
- **E — деградация/ошибки**: suggester-ошибка → logger.exception, движок жив; пустой suggest → пропуск.
- **F — CLI**: ghostwrite [--enforce] (Ctrl+C); ghostwrite-dialogs enable/disable/list/pause-all/resume;
  `*` запрещён (enable '*' → ошибка). Тесты CliRunner+стаб.
- **G — финал**: .env.example (ghostwrite accepted-risk блок), CLAUDE.md/PLAN.md, pytest+ruff.

## Инварианты

asyncio.gather не TaskGroup; no silent failures (logger.exception/warning); LLM только через
factory (ghostwrite.py не импортирует langchain); dry-run дефолт; кэши bounded (OrderedDict);
clock инжектится — тесты без sleep; storage на tmp; agent-тесты на langchain — importorskip,
но GhostwriteEngine/storage тестируются БЕЗ LLM (Suggester инжектируется фейком).

## v1-компромиссы

- Управление — CLI, без web/TUI.
- Лимит/окно и кэш own-id — в памяти (рестарт сбрасывает; paused_until — в storage, переживает).
- Без эскалации/ML — простой Suggester-reuse.
- Приватность: история уходит в LLM (accepted risk, как agent/suggester).
