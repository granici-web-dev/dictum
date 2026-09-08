# CONVENTIONS — idea2backlog

Engineering posture for this repo. Read together with `CLAUDE.md` (rules), `SPEC.md` (behaviour) and `PLAN.md` (current task). `/rigorous` loads this file through the symlinks in `.agents/context/`.

## PRINCIPLES

### Posture
- **The failure that matters is a broken output.** Invalid `issues.json`, duplicate or garbage cards in Trello, invented data in an artifact. Shipping a week later costs less than the team losing trust in the board. Optimise for correctness of contracts first, speed second.
- **Contracts first, plumbing later.** Artifact schemas (`app/models.py`), gate behaviour and the `runs` / `gates` / `publish_log` tables are designed once and treated as stable. Bot handlers and CLI wiring may be rewritten freely.
- **Under time pressure, cut scope, not quality.** Every task still ships with a test and a green `make test`. If it does not fit, split it or move it in `PLAN.md`. No "ship now, test later".

### Abstractions
- **Rule of three.** Extract a helper on the third duplication, not the second. Two similar blocks are cheaper than a wrong abstraction.
- **No Protocol or ABC with one implementation.** One Trello client, one LLM provider, one transcriber: plain module functions or a concrete class. Add a Protocol only when a second real implementation exists, or a test must substitute at an external boundary and monkeypatching the client is not enough.
- **Composition and functions over inheritance.** Inherit only where a framework requires it (`BaseModel`, `BaseSettings`, `DeclarativeBase`). No base classes for stages or clients.

### Comments and naming
- **Module docstring, then silence.** Each module starts with one to three lines: what it owns and which `SPEC.md` § it implements. Inside the code, comment only a non-obvious why (bug workaround, invariant, surprising constraint). Never describe what the code does.
- **Long, specific names.** `consent_confirmed`, `run_stage`, `publish_log`. A name that needs a comment is the wrong name.
- **Abbreviations only when established and local.** `db`, `cfg`, `ctx`, `idx` are fine as local variables. In signatures, fields and module-level names, write the word out. `usr`, `msg`, `resp` are banned.

### Validation and errors
- **Validate at boundaries and at every stage input.** External boundaries (LLM output, Whisper and Trello responses, Telegram input, env) go through Pydantic. Each stage additionally validates its own input artifact (frontmatter, required sections, schema) even though the previous stage produced it: artifacts are re-run with `user_edit` and can be hand-edited. Inside a function, trust the types; mypy strict is the guard. No defensive `if not arg` in internal helpers.
- **Raise, catch once.** Stage code raises. The entry point that started the run is the single catch point: run → `failed`, one Telegram message, one log record. No log-and-continue, no partial publishes.
- **Retries are listed in `SPEC.md` §7 and nowhere else.** Network errors ×2, invalid JSON from decompose ×1 with the validation error fed back. Any other retry is a spec change first.
- **Logging lives at boundaries.** Worker, bot and external clients log with stdlib `logging`. Every LLM call writes one record: run_id, stage, tokens in and out, duration in ms. Pure logic (validation, prompt assembly, cycle detection) does not log. No structlog on MVP.

### Discipline
- **One commit, one decision.** Refactoring goes in its own commit, never mixed with the feature that prompted it. The message answers why (`CLAUDE.md` rule 6).
- **A closed task is a pushed task.** `make test` green and `PLAN.md` ticked is not the end: the work reaches `origin/main` in the same sitting. Twenty-one commits once sat unpushed through a whole day of live runs, and everything that day existed on one laptop. If `origin` is behind by more than the task in hand, say so before starting the next one.
- **TODO only with a PLAN.md id.** `TODO(P1-04): ...`. A TODO without an id is a review blocker.
- **Generated code is edited.** Alembic autogenerate output is reviewed and corrected by hand before commit (names, indexes, data migrations). Never commit a migration you have not read.
- **Never invent data, including in fixtures.** `[уточнить: ...]` instead of a plausible placeholder (`CLAUDE.md` rule 5).

### Reversibility before risk
- **A live API call on the user's keys needs an explicit ok in the chat, each time.** Editing files is free. Spending someone's credits, writing cards to Trello, sending a Telegram message: those leave the machine and cannot be taken back. Approval for one run is not approval for the next, and verifying a change is not a reason to make the call.
- **The local pipeline is disarmed by default.** `ALLOW_LIVE_API=true` in `.env` arms it for a run you mean to pay for; without it `app/stages.py` builds no client and nothing leaves the process. Put it back to `false` when the run is done.

## STACK

Do not change a row without recording the decision in `SPEC.md`.

| Concern | Choice | Pin |
|---|---|---|
| Language / runtime | Python | 3.12 |
| Package manager | uv, `uv.lock` committed | |
| Lint / format | ruff, line length 100 | ≥0.6 |
| Types | mypy `--strict` on `app/` | ≥1.11 |
| Tests | pytest, pytest-asyncio | ≥8 |
| Telegram | python-telegram-bot, long polling on MVP | ≥21 |
| Pipeline execution | A thread in the process that took the request (`asyncio.to_thread` in the bot) | |
| Persistence | PostgreSQL 16, SQLAlchemy 2.0 `Mapped`, Alembic, psycopg 3 | |
| Data models | Pydantic v2 everywhere, pydantic-settings for env | ≥2.8 |
| LLM | Anthropic SDK direct, prompts in `.claude/commands/*.md` | ≥0.40 |
| Transcription | OpenAI Whisper API, ffmpeg on the machine running the bot | |
| Trello | REST via httpx | ≥0.27 |
| HTTP mocking | respx for httpx; `httpx2.MockTransport` for the Anthropic SDK | |
| Local infra | docker compose: postgres | |
| Hosting | Hetzner, EU only; all processing stays in EU | |

### Decisions
- **Sync stages, async bot.** All pipeline code (stages, clients, DB access) is synchronous: `anthropic.Anthropic`, `httpx.Client`, SQLAlchemy sync session. Only `app/bot.py` is async, because python-telegram-bot requires it. Never maintain sync and async variants of the same function.
- **Pydantic everywhere.** Artifact contracts, internal structures, settings: all `BaseModel`. One model style to hold in your head. SQLAlchemy `Mapped` classes stay separate from Pydantic models; convert explicitly at the DB boundary.
- **Prompts are files, not code.** `app/stages.py` reads `.claude/commands/<stage>.md` as the system prompt. Editing a prompt is a product change and gets its own commit.
- **Describe the good answer before bounding the bad one.** A rule of form beats the number that follows it: raising the title limit from 60 to 80 characters only moved the wall, while saying what a title is ("a verb and an object, at most eight words, no subordinate clause") halved their length on the next run. Write the shape first, then the limit as a backstop.
- **State lives in Postgres.** Run status, gates and `publish_log` are rows. Files under `runs/<run_id>/` (or `outputs/` locally) hold artifacts only.

### Rejected
- **Celery and Redis, until a second process exists.** They were pinned from day one for a worker that never got written: `make worker` pointed at a module holding nothing but a docstring, and five live runs went through a thread inside the bot. Brought back on either condition, not before: a second process appears (P4-03 splits the webhook from the executor), or several people run the pipeline at once and one lock in one process stops being the answer.
- **LangChain, LlamaIndex, agent frameworks.** Five prompts and one SDK do not need an orchestration layer; frameworks hide the exact prompt and the token usage we log.
- **Trello SDKs (py-trello and similar).** Six REST endpoints via httpx are easier to test with respx than a wrapper with its own auth and pagination behaviour.
- **Web frameworks (FastAPI, Django) before P4-03.** The bot is the only UI. A webhook endpoint arrives with the Hetzner deploy, not earlier.
- **Run state in JSON files.** Concurrent runs and 24 h gate timeouts need transactions and timestamps, not file locks.

## TESTING

### Worth testing
- **Artifact contracts and validation.** `issues.json` schema, unknown `depends_on`, cycles, `estimate` and `test_hint`; frontmatter of `transcript.md` and `brief.md`; parsing of the PRD "Скоп MVP" table.
- **External clients through mocks.** Trello: list creation, card, checklist and label creation, publish idempotency via the `dictum:<KEY-N> run:<run_id> local:<id>` marker read back from the board, one repeat on 429. Anthropic: prompt assembly, `user_edit` appended, retry on network error, one retry on invalid JSON. Whisper: chunking and merging of long audio.
- **Gates and run status transitions.** Pipeline stops after intake (candidates or low confidence), brief and decompose; consent blocks audio; `auto_approve` skips every gate except consent; 24 h timeout → `stalled`.
- **Prompts and templates exist and have the right shape.** Each `.claude/commands/<stage>.md` exists with frontmatter; `templates/prd_oneshot.md` contains the scope table header.

### Not worth testing
- Pydantic itself. A field with a pattern does not need a "rejects bad pattern" test unless the pattern is ours and subtle.
- python-telegram-bot handler wiring, SQLAlchemy sessions.
- The wording of LLM output. Structure yes, prose no.

### Mocking
- **No test touches the network.** Our own httpx calls (Trello) go through respx. The Anthropic SDK runs on `httpx2`, which respx does not patch, so its tests inject an `httpx2.MockTransport` into the client the production factory builds. Mocking the SDK object instead would leave the client's own settings, retries included, untested. A test that needs a real API key is a bug.
- **Postgres is real.** DB tests run against the docker Postgres from `make up`, are marked `@pytest.mark.db`, and skip when the database is unreachable. No SQLite substitute: dialect differences hide bugs. Register the marker in `pyproject.toml` with the first DB test.
- **Fixtures are files in `fixtures/`**, realistic and honest (rule 5). Prefer a fixture file over a factory in `conftest.py`; add a factory only when three tests build the same object inline.

### Speed and coverage
- No coverage number. Covered where it breaks: contracts, gates, publish.
- Full suite under 10 s without DB tests; a single test under 1 s. DB tests may be slower but must stay marked.

### Layout and naming
- Parallel tree: `tests/test_<module>.py` mirrors `app/<module>.py`.
- Test name: `test_<subject>_<condition_or_outcome>`. Existing: `test_fixture_issues_valid`, `test_all_stage_prompts_exist`. Target shape: `test_publish_skips_an_issue_whose_marker_is_already_on_the_board`, `test_decompose_retries_once_on_invalid_json`, `test_gate_blocks_audio_without_consent`.
- Snapshot tests only for deterministic artifacts rendered by code (`issues.md` from `issues.json`), never for LLM responses. Snapshots live next to the fixtures they derive from.
- No end-to-end tool. The end-to-end check is `make run-text` against `fixtures/idea_text.md` with real keys, run by a human before closing a phase (P1-06).
