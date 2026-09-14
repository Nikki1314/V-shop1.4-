# Security model

This document describes the controls the code actually implements, and states
plainly what is out of scope. V-Shop is a single-tenant Telegram bot for one shop;
its security goals are to keep customer data private, keep the admin panel
restricted to named operators, and make rewards and prices impossible to
manipulate from a Telegram client.

## Trust boundaries

| Actor | Trust | Interface |
|---|---|---|
| Customer | untrusted — every message and callback is input | private chat with the bot |
| Admin | trusted, identified by Telegram user id (`ADMIN_IDS`) | private chat, `/admin` |
| Emergency operator | trusted for one session, identified by Telegram user id plus the emergency password (`EMERGENCY_ADMIN_PASSWORD_HASH`); never added to `ADMIN_IDS` | private chat, `/emergency_admin`, then the same `/admin` panel |
| Manager / reviews groups | notification targets only | outbound messages, no commands, no buttons |
| Operator / host | trusted | `.env`, Docker, PostgreSQL |
| Telegram | trusted for user identity and transport | Bot API over HTTPS (long polling) |

## Identity and admin authorization

- **Identity is Telegram's.** A user is the `from_user.id` of an update, stored as
  `users.telegram_id` (unique). The bot has one password of its own — the
  optional emergency admin secret, held only as a scrypt hash
  (`EMERGENCY_ADMIN_PASSWORD_HASH`) and verified in constant time off the event
  loop (`app/utils/passwords.py`). A successful check opens a time-boxed,
  revocable session in `admin_access_sessions` (`app/services/emergency_admin.py`);
  every attempt is recorded in `admin_access_attempts`, and after
  `EMERGENCY_ADMIN_MAX_FAILED_ATTEMPTS` failures the user is locked out for
  `EMERGENCY_ADMIN_LOCKOUT_MINUTES` — answered like any other failure. Neither
  table holds a credential. `/emergency_admin` (`app/handlers/user/emergency_admin.py`)
  asks for the password (or takes it from `/emergency_admin <password>`),
  deletes the message that carried it (best effort — Telegram and the operator's
  device may already have shown it, hence the advice to rotate after use), commits the
  attempt before answering, and answers every denial with the non-admin's
  "Access denied"; with the hash unset it says nothing. The command performs no
  admin operation: a success only shows the panel, and every later tap passes
  the admin router's gates.
- **Admins are an allow-list, or hold an emergency session.** `ADMIN_IDS` fails
  closed: empty or unparseable means nobody is a configured admin, and a negative
  (group) id can never match. `resolve_admin_grant` (`app/security/admin.py`) is
  the one decision: a configured id is granted from settings alone, without a
  query; anyone else only by an `admin_access_sessions` row that is neither
  revoked nor expired, read on every update, so revocation and expiry take effect
  on the next message. A session ends by expiry, by the operator logging in again
  (which supersedes it) or by revocation in the database; the bot offers no logout
  command. Without a database session the answer is *no*. A grant
  changes nothing in `ADMIN_IDS` and opens the same panel and handlers, and the
  handler receives it as `admin_grant` so an action can be attributed to the
  session that authorized it.
- **Two gates on the admin router.** Router-level `IsAdmin` filters and
  `AdminOnlyMiddleware`, which drops unauthorized updates silently and logs a
  warning with the user id; both call `resolve_admin_grant`, and no handler
  repeats the check. A non-admin sending `/admin` gets an access-denied
  message from the user router; a stranger's tap on a stale admin button is
  dropped without an answer (the fallback router excludes `admin:` callbacks).
- **Private chats only.** `PrivateChatMiddleware` drops every update that is not
  from a private chat before a database session is opened or an error reply
  could be sent; an undeterminable chat fails closed. Broadcasts only reach
  positive (private) chat ids, and outbound group notifications carry no inline
  keyboards.

## Input handling

- **Callbacks carry ids, never values.** Callback data holds namespaced ids
  (`checkout:reward:<id>`, `roulette:spin:<grant_id>`, …). Ids are parsed with
  bounded parsers (`parse_positive_int`, `parse_nonnegative_int`, capped at the
  PostgreSQL integer range) or enum allow-lists before any query; anything
  malformed is answered "this button is no longer valid".
- **Ownership is re-checked server-side.** Cart lines, rewards and spin grants are
  looked up per customer; a reward id offered at checkout is checked against that
  customer's current options. The database backs this up: `roulette_spins`,
  `user_rewards` and `loyalty_transactions` reference grants, spins and rewards by
  `(id, user_id)`, so a row can never point at another customer's entitlement.
- **Money and rewards are computed on the server.** Checkout prices come from the
  database at placement; reward savings are planned by `RewardService` under lock;
  roulette prizes are drawn by `secrets.randbelow`; stamps come only from the
  admin's `Completed` status change. No client input can set a price, a prize, a
  balance or a stamp count.
- **Text input is bounded.** Checkout fields and admin wizard fields are
  length-checked against their columns before they are stored.
- **SQL.** All queries go through SQLAlchemy with bound parameters.
  `tests/test_security_audit.py` fails the build if raw SQL is built from anything
  but a literal.

## Output encoding

Every message is sent with HTML parse mode, and every user- or database-supplied
value is escaped with `e()` (`app/utils/html.py`) — customer names and addresses
in the manager alert, product names in the cart and admin screens, admin search
queries. Telegram rejects a message it cannot parse, so `tests/test_html_escaping.py`
feeds names containing `<` and `&` through the real dispatcher with a fake
Telegram that rejects invalid HTML.

## Rewards, idempotency and concurrency

- Every earning or spending event references its source row under a unique
  constraint, so replays, restarts and duplicate Telegram updates cannot book
  anything twice.
- Every loyalty mutation locks the customer's account row first; operations take
  their locks in one global order, and no Telegram call is made while a loyalty
  lock is held (checked by `tests/test_loyalty_concurrency.py`).
- Reward records are re-verified before they are stored (a free bottle at its
  product's price, a discount at exactly its percentage), and
  `python -m app.verify_deployment` cross-checks the tables after a deploy.

Details: [Architecture](architecture.md#loyalty-transactions-and-concurrency),
[Loyalty](loyalty.md), [Roulette](roulette.md), [Referrals](referrals.md).

## Manual stamp credits

- **Who.** The 🪪 Loyalty wizard lives inside the admin router, so only a
  configured admin or an active emergency session reaches it; a stranger's
  `/admin_adjust_stamps` or copied button is dropped without an answer. The
  `admin_grant` the router injects is recorded on every credit's author row.
- **Whom.** The customer is named by Telegram id (exact) or `@username`
  (case-insensitive, against the handle the customer last showed the bot,
  refused when missing or stored for several customers), resolved on the server
  and re-resolved by id right before booking. An operator cannot credit
  themselves.
- **How much.** A whole number from 1 to `LOYALTY_ADMIN_MAX_STAMP_ADJUSTMENT`,
  validated at the step and again by the service; nothing negative, nothing zero.
- **The button.** The confirmation carries only a fresh operation id; the
  customer and the amount live in the operator's server-side FSM data. A second
  tap, two taps at once, an earlier screen's button, a forged id, another
  operator's copied button or a screen that outlived a restart credits nothing.
- **The write.** One `adjustment` ledger row through `LoyaltyService.adjust`
  under the customer's account lock, plus one `loyalty_stamp_adjustments`
  author row, in one transaction, committed before the operator is answered;
  the operation id is unique, so the same request booked twice is one credit.
  A credit never mints a reward and never counts as a purchase.
- **Silence.** No message to the customer, none to the manager chat, none to
  the other admins: the customer sees the stamps on their card. The sender
  modules state this and `tests/test_adjustment_notifications.py` proves it.
- **Audit.** Every credit is explainable from the ledger row and its author row
  (operator, `configured` or `break_glass`, session id, operation id, time);
  `python -m app.verify_deployment` reports adjustment rows without an author
  and author rows that disagree with their ledger row.

## Referral tokens

Codes are 72-bit random values (`secrets.token_urlsafe(9)`), not derived from any
id, validated against a strict pattern before lookup, never logged, and never
expiring. The newcomer's replies are identical for a valid, unknown or malformed
code, so the bot is not an oracle for which codes exist. Self-referral, loops and
re-attribution are refused, and nothing is paid until the referred customer's
first paid order is completed by an admin.

## Secrets and configuration

- Secrets (`BOT_TOKEN`, database credentials, the emergency admin password hash) live in `.env`, which is excluded by
  `.gitignore` and `.dockerignore`; `.env.example` holds placeholders only.
- The database URL is logged with its password masked; the bot token is never
  logged.
- Every SQLAlchemy engine is created with `hide_parameters=True`, so bound
  parameters — names, phones, addresses, referral codes — never appear in SQL echo
  or in logged exceptions (`tests/test_security_audit.py` checks every engine).
- The request log records update id, kind, user id and duration — never message
  text or payloads.
- `TELEGRAM_SSL_VERIFY` stays `true` in production. `docker/ca-certificates/`
  lets the **image build** trust a TLS-intercepting proxy; certificates there are
  git-ignored and never used by the running bot.

## Database access

PostgreSQL runs in the Compose network and is published only on `127.0.0.1`; for
production the port mapping can be removed entirely. Credentials come from
`POSTGRES_USER` / `POSTGRES_PASSWORD` (the default `vshop` is for development
only). The bot container reaches the database as `db:5432`.

## Out of scope and known limitations

| Area | Status |
|---|---|
| Flood / rate limiting | No general per-user throttle and no limit on concurrent update handling. The one exception is emergency admin authentication, which locks a user out after repeated failures. Broadcasts are paced and honour Telegram's `RetryAfter`. |
| Webhooks | Not used — the bot long-polls, so no inbound HTTP endpoint exists. |
| Encryption at rest, backups | Delegated to the host; backups are a documented manual procedure ([Deployment](deployment.md#backups)). |
| Container hardening | The image runs as root and is not scanned in a pipeline. |
| Database error details | `hide_parameters` hides bound values, but PostgreSQL's own `DETAIL` text of a constraint violation could still reach the log. No normal flow is known to trigger one. |
| Multi-account abuse | One person operating several Telegram accounts that each place real orders is not detected; admins complete orders manually. |
| Monitoring / alerting | Logs only; no metrics or alerting are included. |

## Tests

`tests/test_security_audit.py`, `tests/test_group_isolation_security.py`,
`tests/test_private_chat_isolation.py`, `tests/test_loyalty_attacks.py`,
`tests/test_html_escaping.py`, `tests/test_admin_guards.py`,
`tests/test_admin_authorization.py`, `tests/test_emergency_admin_security.py`,
`tests/test_admin_stamp_wizard_security.py` and
`tests/test_adjustment_notifications.py`. See [Testing](testing.md).
