# Admin guide

The whole shop is operated from inside Telegram. Admin features are available
only in a **private chat** with the bot, and only to Telegram user IDs listed in
`ADMIN_IDS`.

## Access

1. Put your numeric Telegram ID in `.env` → `ADMIN_IDS` (several are
   comma-separated).
2. Apply it: `docker compose up -d` (or restart the process).
3. Send `/admin` to the bot.

The admin panel keyboard:

| Button | Section |
|---|---|
| 📦 Products | add products; list, edit, enable/disable, delete |
| 📂 Categories | categories and their brands |
| 📋 Orders | new and completed orders, search, status changes |
| 📢 Broadcast | message every registered customer |
| 📊 Statistics | orders, revenue and product rankings |
| 🪪 Loyalty | credit stamps to a customer by hand |
| ⚙ Settings | informational placeholder — configuration is done through environment variables |

**Authorization model.** The admin router is gated twice: `IsAdmin` filters and
`AdminOnlyMiddleware`, which drops any update from a non-admin silently and logs
a warning. A non-admin who sends `/admin` receives an access-denied message and
never sees admin controls; a stranger's tap on an admin button is ignored. An
empty `ADMIN_IDS` means nobody has a permanent admin seat. Group chats are ignored
entirely. Besides `ADMIN_IDS`, an active emergency session
([Configuration](configuration.md#emergency-admin-access)) passes the same gates
and opens this same panel until it expires or is revoked; it never changes
`ADMIN_IDS` and never receives new-order alerts.

**Emergency access.** If your Telegram ID is not in `ADMIN_IDS` but the
operator has set `EMERGENCY_ADMIN_PASSWORD_HASH`, send `/emergency_admin`, then
the password as your next message, or in one line as `/emergency_admin <password>`
(the bot deletes that message — best effort: Telegram, and a notification on
your phone, may already have shown it, so rotate the password after use). A correct
password opens this same panel for `EMERGENCY_ADMIN_SESSION_TTL_MINUTES`; a wrong
one is answered "Access denied", and repeated failures lock you out for
`EMERGENCY_ADMIN_LOCKOUT_MINUTES`. Sending the command again later opens a new
session and ends the previous one. There is no logout command: a session ends
when it expires, when you log in again, or when whoever runs the database
revokes it. Emergency sessions never receive new-order alerts and never change
`ADMIN_IDS` — nobody becomes a permanent administrator this way. Every attempt
and every session is recorded (`admin_access_attempts`, `admin_access_sessions`)
for whoever runs the database to review. See
[Configuration](configuration.md#emergency-admin-access).

**Wizards.** While a wizard is active (adding a product, renaming a category, a
broadcast, …), tapping another admin menu button is blocked until you **Cancel**
or finish the flow. Wizard state is kept in memory: after a bot restart, start
the flow again.

Admin screens are shown in the admin's own language (chosen at `/start`, like any
customer). New-order alerts are the exception — they are English by design.

---

## Products

### Add product

📦 Products → **Add product**:

1. Send a **photo** (required).
2. Enter the name in Russian, English, German and Ukrainian — each at most 255
   characters.
3. Enter the description in the same four languages.
4. Pick a **category**, then a **brand** within it. A category without brands
   stops the wizard: create a brand first (see [Brands](#brands)).
5. Enter flavor (at most 255 characters), volume and nicotine strength (at most 64
   each).
6. Enter the **price**: digits with up to two decimals, `12.50` or `12,50`,
   between 0.01 and 99,999,999.99. Scientific notation is rejected.
7. Review the preview → **Confirm**. A double tap on Confirm creates the product
   once.

### Manage products

📦 Products → **Manage** opens a paginated list; open a product card for:

- **Edit** — the full wizard: photo, names and descriptions in all four languages,
  category, brand, flavor, volume, nicotine, price. **Skip** keeps the current
  value of a step.
- **Edit price** — the price only.
- **Edit description** — Russian, English and German. The Ukrainian description
  is changed through **Edit**.
- **Enable** / **Disable** — shows or hides the product in the catalog
  (`is_active`).
- **Delete** — asks for confirmation, and is refused if the product appears in
  any order: order history must stay readable.

Product images are stored as Telegram `file_id`s of the uploaded photo.

---

## Categories and brands

The catalog has three levels: **category → brand → product**.

### Categories

📂 Categories lists the categories in their display order.

- **Create** — enter the name in Russian, English, German and Ukrainian. The
  Russian name must be unique.
- Open a category to **edit a name** (pick the language), **activate /
  deactivate** it, **move it up / down** (the order customers see), open its
  **brands**, or **delete** it. Deleting asks for confirmation and is refused
  while the category still has brands or products.

### Brands

From a category, open its brands to:

- **create** a brand (four localized names);
- **edit a name** in one language;
- **activate / deactivate** it;
- **move it up / down**;
- **move it to another category**;
- **delete** it — refused while it still has products.

### What "on sale" means

A product is on sale only when **all three** are active: the product, its
category, and — if it has one — its brand. Disabling a category or a brand
therefore takes every product under it off the shelf without touching the
product rows, and re-enabling puts them straight back.

The rule is defined once, in `app/repositories/visibility.py`, and applies
everywhere the question is asked: catalog browsing, the checkout guard (an item
that went off sale while sitting in a cart is refused), and the statistics
top/bottom product rankings. Products created before the category → brand →
product hierarchy carry no brand and are judged on their category alone.

---

## Orders

📋 Orders offers **New orders** and **Completed orders** (both paginated) and
**Search**:

- an order number finds that order;
- any other text searches customer names and phone numbers (substring,
  case-insensitive).

The order card shows the status, date, customer name, Telegram username and ID,
city, delivery method, address, preferred time, phone, payment method, items and
total. When a loyalty reward was used, a **Reward used** line appears under the
items: a free bottle is the item listed at 0.00, and a discount is already taken
off the total — **charge the total shown**.

### Changing status

Only the moves allowed from the current status are offered:

| Current status | Buttons |
|---|---|
| `New` | 👍 Accept · ❌ Cancel order |
| `Accepted` | 📦 Ship · ❌ Cancel order |
| `Shipped` | ✅ Complete · ❌ Cancel order |
| `Completed` | none — terminal |
| `Cancelled` | ↩️ Reopen order (back to `New`) |

- The customer is told about `Accepted`, `Shipped`, `Completed` and `Cancelled`,
  in their own language. Reopening a cancelled order is an internal correction
  and sends nothing.
- **Completing** an order books the customer's loyalty stamps, any roulette spin
  it earns, and a first order's referral bonus — automatically, in the same step.
- A button from an outdated screen that asks for an illegal move is refused, and
  the card is redrawn with the current status. Two admins tapping at the same
  moment move the order once, and the customer is told once.

New orders notify `MANAGER_CHAT_ID` (a group or a private chat) and each chat in
`ADMIN_IDS`, without duplicates. These alerts are English and carry no buttons.

---

## Loyalty

There is nothing to operate by hand:

- stamps, milestone roulette spins and referral payouts are booked when an order
  is marked **Completed**;
- customers claim free bottles on 🪪 My Stamp Card and choose rewards at
  checkout;
- a reward used on an order that is later cancelled stays used (owner decision).

**Crediting stamps by hand — a manual loyalty adjustment** (🪪 Loyalty → ➕ Credit stamps, or
`/admin_adjust_stamps`): send the customer's Telegram ID or `@username`; the bot
shows who that is and their current stamps; send how many to add (a whole number,
at most `LOYALTY_ADMIN_MAX_STAMP_ADJUSTMENT`); confirm. The credit is one ledger
row, recorded with you as its author (and your emergency session, if you hold
one). It does not count as a purchase, issues no reward by itself — if the card
becomes full the customer claims the free bottle on it, as after a purchase —
and sends no message to the customer or the manager chat. A username is matched
against what the customer last showed the bot; if it is shared by several
records, use the Telegram ID. You cannot credit yourself. Tapping Confirm twice,
or a Confirm button from an earlier screen or after a restart, credits nothing
more.

There is no admin screen for loyalty settings: the rules are environment
variables (see [Configuration](configuration.md#loyalty-stamp-card)), and
`python -m app.verify_deployment` reports the loyalty integrity checks. How the
programme works: [Loyalty](loyalty.md), [Roulette](roulette.md),
[Referrals](referrals.md).

---

## Broadcast

1. 📢 Broadcast shows the number of recipients; start a new broadcast.
2. Send **text**, or a **photo** with an optional caption.
3. Review the preview.
4. **Confirm** to send.

Progress updates appear as the fan-out runs. Sending is paced, and Telegram's
`RetryAfter` responses are honoured, to avoid flood errors. Failed recipients are
summarized at the end. A double tap on Confirm sends once.

---

## Statistics

`📊 Statistics` on the admin panel. Private admin chats only — the button is
absent from every customer keyboard, the router sits behind `IsAdmin` plus
`AdminOnlyMiddleware`, and group traffic is dropped before it reaches any
handler.

The dashboard is one message:

| Section | Contents |
|---|---|
| General | users, categories, subcategories, products, total orders |
| Orders | all time / this month / last month, each total ✅ completed ❌ cancelled |
| Revenue | all time / this month / last month, **completed orders only** |
| Products | top 3 most ordered and top 3 least ordered |

Product rankings count **distinct completed orders** containing the product, not
units sold: an order holding five of an item counts once. Only products that are
on sale are ranked (see [What "on sale" means](#what-on-sale-means)), and a
product with zero completed orders qualifies for the bottom list — that is what
the list is for.

Month boundaries follow `APP_TIMEZONE` (default `Europe/Berlin`), not the server
clock, so an order placed at 00:30 local on the 1st belongs to the new month.
The header shows the month and zone the figures were cut with.

Money is punctuated the way the reader's language writes numbers — `€1,234.56`
in English, `1.234,56 €` in German, `1 234,56 €` in Russian and Ukrainian. The
symbol itself comes from `CURRENCY_SYMBOL`.

Product names longer than 26 characters are trimmed at the nearest word with an
ellipsis, so a ranked list stays one line per entry.

`🔄 Refresh` redraws in place. If nothing has changed since the last tap the
message stays as it is — Telegram rejects an unchanged edit, and that is not an
error.

Empty states are distinct on purpose: an empty catalog says there are no
products, while a stocked catalog with no completed sales says nothing has sold
yet.

---

## Settings

⚙ Settings currently shows an informational message only. Every setting — admin
IDs, notification chats, loyalty rules, roulette weights — is an environment
variable; see [Configuration](configuration.md).

---

## Customer-facing reminder

After catalog changes:

- disabled products, and products under a disabled category or brand, disappear
  from the customer catalog;
- category and brand order follows the admin's ordering;
- cart lines for deleted products are removed (a delete only succeeds when the
  product is in no order).

---

## Operational tips

- Keep `ADMIN_IDS` small and trusted — admins can broadcast to every customer.
- Use a dedicated manager group for `MANAGER_CHAT_ID` so order alerts are shared.
- If the bot restarts mid-wizard, send `/admin` again and restart the flow (FSM
  state is in memory).
- If order alerts stop arriving after changing group settings, the group may have
  become a supergroup with a new ID — see
  [Deployment](deployment.md#order-notifications-stop-arriving-in-the-manager-group).
