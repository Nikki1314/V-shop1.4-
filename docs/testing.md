# Testing

The test suite is the project's main quality gate. At the time of writing it
collects **1,936 tests**; 39 of them are PostgreSQL concurrency and deployment
tests that run only when a scratch PostgreSQL database is provided.

## Tooling

| Tool | Role |
|---|---|
| pytest + pytest-asyncio | test runner; `asyncio_mode = "auto"` (configured in `pyproject.toml`) |
| aiosqlite | in-memory SQLite for the default run — no database or bot token needed |
| PostgreSQL 16 | the opt-in suites that prove locking, races and migrations |
| ruff | lint (`E`, `F`, `I`, `UP`, `B`) and formatting |
| mypy | strict type checking of `app/` |
| Alembic | `alembic check` compares the migrated schema with the ORM models |

`tests/conftest.py` strips every `Settings` environment variable and disables the
`.env` file for the whole session, so tests never read a developer's local
configuration.

## Test layers

| Layer | How it works | Examples |
|---|---|---|
| **Unit** | pure functions and services against in-memory SQLite | `test_validators.py`, `test_roulette_engine.py`, `test_loyalty_ledger.py` |
| **Integration** | handlers called with spy Telegram objects, real services and database | `test_checkout_handlers.py`, `test_stamp_card_ui.py`, `test_admin_statistics.py` |
| **End-to-end** | real Telegram updates fed through the **production dispatcher** — every middleware, filter, router and FSM state — with a fake Bot API session (`tests/production_bot.py`) | `test_loyalty_e2e_qa.py`, `test_loyalty_journeys.py`, `test_shop_lifecycle.py`, `test_loyalty_languages.py` |
| **Concurrency (PostgreSQL)** | real transactions racing each other; checks that no Bot API call is made while a loyalty lock is held | `test_loyalty_postgres.py`, `test_loyalty_journeys_postgres.py`, `test_loyalty_concurrency.py` |
| **Deployment (PostgreSQL)** | the real migrations applied to a pre-loyalty shop, then redeploys and restarts; `alembic check` on the migrated schema | `test_loyalty_activation_postgres.py` |
| **Migrations (static)** | `upgrade()` never drops or deletes — including through helpers, `batch_op`, `sa.text` and constants; linear single-headed chain; every model table migrated | `test_migrations.py`, `test_loyalty_schema.py` |
| **Security** | callback parsing bounds, admin fail-closed, raw-SQL guards, `hide_parameters`, cross-customer access, HTML escaping, group isolation | `test_security_audit.py`, `test_group_isolation_security.py`, `test_private_chat_isolation.py`, `test_loyalty_attacks.py`, `test_html_escaping.py` |
| **Localization** | identical key sets, every template renders, formal address, consistent terminology, and every screen of the customer journey fits a phone in each language | `test_localization.py`, `test_localization_quality.py`, `test_translation_parity.py`, `test_ukrainian.py`, `test_loyalty_languages.py` |
| **Documentation** | settings, tables, indexes, statuses, migrations and the middleware order must match these documents | `test_documentation.py` |

Worth knowing:

- `tests/production_bot.py` composes the real router tree once and moves it
  between dispatchers (`mount()`), because aiogram allows a router only one
  parent per process.
- `tests/test_html_escaping.py` uses a fake Telegram that **rejects HTML the way
  the Bot API does**, so a missing `e()` fails the test instead of silently
  showing nothing to a customer.
- SQLite ignores `SELECT … FOR UPDATE`. On every default run,
  `tests/test_loyalty_scenarios.py` and `tests/test_loyalty_guards.py` check that
  each loyalty path *asks* for its locks in the right order; only the PostgreSQL
  suites prove the locks *hold*.

## Running the tests

```bash
pip install -r requirements-dev.txt
pip install -e .

python -m pytest tests -q                                  # everything that runs on SQLite
python -m pytest tests/test_checkout.py -q                 # one file
python -m pytest tests/test_checkout.py::test_build_checkout_summary_contains_core_fields -q
```

### PostgreSQL suites

They need `VSHOP_TEST_POSTGRES_URL` pointing at an **empty** database whose name
ends in `_test` — each suite creates and drops the schema, and refuses anything
else. A throwaway container is enough:

```bash
docker run -d --rm --name vshop-test-pg \
  -e POSTGRES_USER=vshop -e POSTGRES_PASSWORD=vshop -e POSTGRES_DB=vshop_test \
  --tmpfs /var/lib/postgresql/data -p 127.0.0.1:55432:5432 postgres:16-alpine

VSHOP_TEST_POSTGRES_URL=postgresql+asyncpg://vshop:vshop@127.0.0.1:55432/vshop_test \
VSHOP_TEST_NO_SKIPS=1 python -m pytest tests

docker stop vshop-test-pg
```

`VSHOP_TEST_NO_SKIPS=1` fails the session if any test is skipped, so a run that
silently lost its PostgreSQL connection cannot pass. Never point these variables
at a production database.

| Variable | Used by | Meaning |
|---|---|---|
| `VSHOP_TEST_POSTGRES_URL` | the four PostgreSQL suites | async SQLAlchemy URL of an empty `*_test` database; unset → those suites skip |
| `VSHOP_TEST_NO_SKIPS` | `tests/conftest.py` | `1` → any skipped test fails the session |

## Quality gate

There is **no hosted CI pipeline** in this repository. A release is verified with
the following commands, from a clean checkout:

```bash
ruff format --check .
ruff check .
mypy app
VSHOP_TEST_POSTGRES_URL=… VSHOP_TEST_NO_SKIPS=1 python -m pytest tests   # full suite, nothing skipped
alembic upgrade head && alembic downgrade base && alembic upgrade head && alembic check
python -m build                                                          # wheel + sdist (needs `pip install build`)
docker build --no-cache -t vshop:local .
```

The [production checklist](deployment.md#production-checklist) requires this
gate to pass before a deploy. Running the same commands in a hosted CI service is
listed under future improvements in the [README](../README.md#known-limitations-and-future-work).
