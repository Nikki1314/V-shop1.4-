# Changelog

Notable changes, newest first. The repository has no version tags; entries are
dated by their commits.

## 2026-09-14 — Emergency admin access and manual stamp credits

### Added

- **Emergency (break-glass) admin access.** `/emergency_admin` asks an operator
  who is not in `ADMIN_IDS` for a password, deletes the message that carried it,
  and checks it against `EMERGENCY_ADMIN_PASSWORD_HASH` — an scrypt hash made with
  `python -m app.hash_emergency_password` (twelve characters or more), verified
  in constant time in a worker thread. Success opens one session lasting
  `EMERGENCY_ADMIN_SESSION_TTL_MINUTES` (`admin_access_sessions`, migration
  `d7a3f9c2e8b1`), revoking the operator's earlier one; every attempt is recorded
  (`admin_access_attempts`, migration `e8b2c4d6f1a3`) and after
  `EMERGENCY_ADMIN_MAX_FAILED_ATTEMPTS` failures the account is locked out for
  `EMERGENCY_ADMIN_LOCKOUT_MINUTES`. Every denial reads like a non-admin's
  `/admin`; with the hash unset the command is silent. No credential is stored or
  logged, and `ADMIN_IDS` is never changed.
- **Centralized authorization.** `resolve_admin_grant` decides admin access once
  for `IsAdmin`, `IsNotAdmin` and `AdminOnlyMiddleware`: `ADMIN_IDS` from settings
  alone, as before, or an active emergency session read on every update — so
  expiry and revocation take effect on the next message. Handlers receive the
  decision as `admin_grant` and never check access themselves.
- **🪪 Loyalty: manual stamp credits.** `/admin_adjust_stamps` or the new panel
  button: the customer by Telegram ID or `@username` (case-insensitive, refused
  when missing or shared), their card with the current stamps, a whole number up
  to `LOYALTY_ADMIN_MAX_STAMP_ADJUSTMENT`, then a confirmation whose button
  carries only an operation id. The credit is one `adjustment` row through the
  existing ledger plus a `loyalty_stamp_adjustments` author row (operator,
  authority, session, operation id; migration `f3c7a1d9e2b5`), booked once per
  screen however often it is tapped. It issues no reward, counts no purchase and
  sends no message to the customer, the manager chat or the admins; the
  deployment health report gains an `audit` section for adjustments without an
  author. Localized in Russian, English, German and Ukrainian.

### Fixed

- **`keyed_lock` and event loops.** The lock registry starts afresh when the
  running event loop changes, so a key contended in one test's loop is never
  handed to the next test as a lock it cannot wait on. No behaviour change for
  the bot, which runs one loop for its whole life.

## 2026-09-12 — Loyalty, roulette and referral release

### Added

- **Stamp Card.** One stamp per €20 of a completed order's charged total (orders
  placed after launch, charged above €0). 10 stamps unlock a free bottle up to €20,
  claimed with a versioned one-tap claim. The append-only stamp ledger is the
  source of truth.
- **Lucky Roulette.** A one-time welcome spin, a spin on every 5th qualifying
  purchase, and a referral spin. The prizes are +1/+2 stamps, 5%/10% discounts and a
  free bottle, drawn on the server with `secrets.randbelow` using configurable
  weights. Each spin is consumed, recorded and paid out in one transaction, and
  replays show the saved result.
- **Referral programme.** Personal deep links with 72-bit random codes. Only a
  never-ordered customer can be attributed; self-referrals and loops are refused.
  +2 stamps for each side and a spin for the referrer are paid once, when the
  friend's first paid order completes.
- **Reward engine.** Free bottles and discounts are chosen at checkout and
  re-planned under lock at confirmation; the redemption record shows what each
  reward was worth and paid for. One reward per order; rewards never expire.
- **Startup loyalty activation.** An idempotent backfill of loyalty accounts and
  welcome spins for existing customers on every start.
- **Operations.** `python -m app.verify_deployment` reports loyalty integrity
  cross-checks, and the manager alert and admin order card show a "Reward used"
  line.
- **Localization.** All loyalty screens in Russian, English, German and
  Ukrainian, with CLDR plurals, inflected ordinals and a formal German voice.
- **Tests.**
  - PostgreSQL concurrency suites, including a check that no Telegram call is
    made while a lock is held.
  - A deployment test on an existing shop.
  - End-to-end journeys through the production dispatcher in every language.
  - Localization quality and phone-fit checks.
  - Telegram-strict HTML escaping tests.
  - `VSHOP_TEST_NO_SKIPS=1` for release runs.
- **Documentation.** Dedicated loyalty, roulette, referral, security and testing
  documents, plus architecture and ER diagrams.

### Changed

- **The PostgreSQL volume is `external`**, and `POSTGRES_VOLUME_NAME` is
  **required**. Compose no longer creates the volume or falls back to a default
  name, so a misconfigured deploy refuses to start instead of attaching to an
  empty database.
- **Order status changes** lock the order row and re-read the status; customers
  and referral news are notified only when a request actually moved the order.
- **Translation helpers.** `t()`, `plural()` and `translate()` take their key
  positional-only.
- **`.env.example`** defaults to `APP_ENV=production` and a local `DATABASE_URL`.

### Fixed

- Product names are HTML-escaped on the cart screen, in the admin product
  previews and in the order search.
- Renaming a category or brand in one language no longer fails.
- Admin product text fields are limited to their column length.
- The admin order card no longer shows a literal `\n`.
- The in-process lock registry can no longer split a lock while it is being
  handed over.

### Migrations

| Revision | Change |
|---|---|
| `3b9d6f2a8c14` | six loyalty tables, plus a backfill of accounts and welcome spins |
| `8e4c1a7b2d95` | `orders.loyalty_eligible` (existing orders stay ineligible) |
| `c5d2e8f1a6b3` | reward redemption record |

All three only add tables and columns. Their downgrades refuse while customer
loyalty data exists; see [Deployment](docs/deployment.md#migrations).

### Upgrade notes

Set `POSTGRES_VOLUME_NAME` in `.env` to the volume that already holds your
database **before** deploying — see
[Upgrading an existing deployment](docs/deployment.md#upgrading-an-existing-deployment).

## 2026-08-03 – 2026-08-21 — Shop foundation

- The initial Telegram shop: catalog, cart, checkout, orders, admin panel,
  broadcasts and four-language localization.
- The catalog is preserved across bot updates: the Compose project and volume
  names are pinned, and the database identity is logged at startup.
- A hierarchical catalog: category → brand (subcategory) → product, with
  localized names.
- A hardened order workflow: the full status lifecycle with undo, customer status
  notifications, and private-chat isolation.
- Admin statistics: orders, completed revenue and product rankings, with month
  boundaries in the shop's time zone.
- Production hardening across checkout, statistics and deployment, plus manager
  group notifications.
