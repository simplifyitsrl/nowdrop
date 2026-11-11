# NowDrop — build spec for Claude Code

> Working name: **NowDrop** (placeholder, rename freely).
> Submission for the "PayPal AI Hackathon: Build what's next with PayPal and AI" (Devpost).
> **Hard deadline: Thursday Nov 12, 2026, 12:00 pm Pacific (16:00 La Paz).** Target: everything working by Wed night, polish + deploy Thursday morning.

## 0. How to work

- You are building a small but complete, working product. A finished narrow flow beats a broad half-built one. Mockups and non-functional demos are disqualified.
- Build in the milestone order of section 9. After each milestone: run the tests, run the app, commit.
- Everything a judge sees must be in **English**: UI, README, code comments, commit messages.
- Never commit secrets. Use `.env` (git-ignored) and keep `.env.example` up to date.
- Do not add features outside this spec until every milestone M1–M5 passes. Stretch goals are in section 10.
- If a PayPal sandbox call behaves differently from this spec, trust the live API response, adapt, and note it in `docs/NOTES.md`.

## 1. The product in one paragraph

Live-stream sellers (TikTok, Instagram, Facebook Live) sell by saying "who wants it?" and buyers comment "mine". Then the seller chases payments by hand, and many "winners" never pay. NowDrop removes that: the seller describes a product out loud and says the trigger word **"now"** — an AI turns the speech into a live listing in seconds — and **the first buyer whose payment is secured wins**. Everyone who arrives late is never charged. The order is recorded under the winner's name automatically.

It is not an auction. Price is fixed. Speed decides.

NowDrop does not integrate with TikTok. It runs on the seller's phone or laptop next to the live stream and listens through that device's microphone. Buyers use one permanent link (pinned in the stream chat or bio) that always shows the current drop.

## 2. Hackathon constraints that shape the build

- PayPal must be **central**; AI must be **meaningful**. Both are in the core loop here (sections 5 and 6).
- Use the free **PayPal sandbox** only.
- Judges must be able to run it: hosted demo URL **and** full setup instructions in the README.
- Public repo with an open-source licence file at the root (MIT).
- Judging criteria, equally weighted: Technological Implementation, Design, Potential Impact, Innovation, Presentation. A coherent product experience counts — do not ship unstyled pages.

## 3. Stack

- Python 3.11+, **FastAPI**, Uvicorn (single worker — see 7.4).
- **SQLite** via the standard `sqlite3` module (WAL mode). No ORM needed.
- Server-rendered **Jinja2** templates + vanilla JS. No frontend build step.
- **PayPal REST API** (Orders v2, Payments v2) called with `httpx`, plus the **PayPal JS SDK** buttons on the buyer page.
- **Anthropic API** (`anthropic` Python SDK) for the LLM. Model name comes from `LLM_MODEL`.
- Browser **Web Speech API** (`SpeechRecognition`) for speech-to-text. Chrome only is acceptable; show a clear message on unsupported browsers.
- **Server-Sent Events** for live updates.
- `segno` for QR codes. `pytest` + `pytest-asyncio` for tests.

## 4. Pages

| Route | Who | What |
|---|---|---|
| `/` | Anyone | Landing: what it is, a "Start a live show" button, a short "How to test this as a judge" box. |
| `/seller/{show_id}` | Seller | Seller console (section 4.1). Access by unguessable show id in the URL; no login. |
| `/s/{show_slug}` | Buyers | The permanent show link. Always displays the current live drop and updates by SSE (section 4.2). |
| `/seller/{show_id}/recap` | Seller | AI recap after the show ends (section 6.3). |

### 4.1 Seller console

- Big mic button: start/stop listening. Live transcript visible.
- Language selector for recognition: English (`en-US`) and Spanish (`es-419`). Trigger words: "now" and "ahora" (configurable via env).
- A **text box fallback**: type the description and press "Drop it". Required — judges may not have a mic, and it makes testing deterministic.
- Optional photo: file/camera input attached to the next drop.
- "Current drop" card: title, price, units left, status, elapsed time, button to close it early.
- The show link and a large QR code for `/s/{show_slug}`.
- Live order feed: each winner appears instantly (name, item, amount, time).
- "End show" button → goes to the recap page.

### 4.2 Buyer show page

- Mobile-first. This is opened from a phone during a live stream.
- States: *waiting for the next drop* / *live drop* (image, title, short description, price, units left, PayPal button) / *sold out* / *show ended*.
- When a new drop goes live the page switches to it without reload.
- After paying, the buyer sees one of two clear results:
  - **Won:** "You got it!" with order number and item.
  - **Lost:** "Sold out — someone was faster. You were not charged."
- Units-left counter updates live for everyone.

## 5. PayPal flow (the core)

Base URL from `PAYPAL_BASE_URL` (default `https://api-m.sandbox.paypal.com`). Get an OAuth token with client credentials at `/v1/oauth2/token`; cache it until shortly before expiry.

Load the JS SDK on the buyer page with `client-id`, `currency`, and `intent=authorize`.

1. **createOrder** (button click) → `POST /api/drops/{drop_id}/orders`
   - Server checks the drop is live and has units left (soft check; fail fast with a "sold out" message).
   - Server creates a PayPal order: `POST /v2/checkout/orders` with `intent: "AUTHORIZE"`, one purchase unit, amount taken **from the database, never from the client**, `custom_id` = drop id, description = drop title.
   - Store an `attempts` row (status `created`). Return the PayPal order id.
2. **onApprove** → `POST /api/orders/{paypal_order_id}/claim`
   1. `POST /v2/checkout/orders/{id}/authorize` — funds are now reserved. Read the authorization id from `purchase_units[0].payments.authorizations[0].id` and the payer name/email from the response.
   2. **Atomic claim:** `UPDATE drops SET units_left = units_left - 1 WHERE id = ? AND status = 'live' AND units_left > 0`. Winner if `rowcount == 1`.
   3. **Winner:** `POST /v2/payments/authorizations/{auth_id}/capture`.
      - Capture OK → attempt status `won`, create the `orders` row, publish SSE events. If `units_left` reached 0, set drop status `sold_out`.
      - Capture fails → give the unit back (`units_left + 1`, status back to `live`), attempt status `capture_failed`, tell the buyer the payment failed and nothing was charged.
   4. **Loser:** `POST /v2/payments/authorizations/{auth_id}/void` → attempt status `lost`. Return the "you were not charged" result.
3. **onCancel / onError** → mark the attempt `abandoned`. No PayPal call needed.

Rules:

- The claim endpoint must be **idempotent** per PayPal order id: a repeated call returns the stored result and makes no new PayPal calls.
- Send a `PayPal-Request-Id` header on authorize, capture and void.
- Log every PayPal call (endpoint, status, debug id) to a `paypal_log` table; show the last entries in a collapsible "PayPal activity" panel on the seller console. Judges like seeing the real calls.
- Refund endpoint for the seller: `POST /api/orders/{id}/refund` → `POST /v2/payments/captures/{capture_id}/refund`. One button per order in the feed.

Why authorize first, then claim: the winner is the first buyer **whose funds are actually secured**, so a declined payment never wastes a unit, and losers get a void instead of a charge-and-refund.

## 6. AI features

All LLM calls go through one module (`app/ai.py`) with a fake implementation for tests.

### 6.1 Speech → drop (core)

- The browser keeps a rolling transcript. When a final recognition result contains a trigger word, it sends the text since the previous drop to `POST /api/shows/{show_id}/drops/from-speech` (the text box fallback posts to the same endpoint).
- The LLM extracts structured data using tool use / JSON output:
  `title`, `description` (one or two selling sentences, same language as the seller), `price` (number), `currency`, `quantity` (default 1), `attributes` (size, colour, etc. as key/value), `confidence`, `missing` (list).
- It must handle casual speech: "tengo tres a quince dólares", "fifteen bucks, last two", filler words, self-corrections ("twenty… no, eighteen").
- If `price` is missing or the result is not a product, do **not** publish: return what is missing and show it on the seller console ("I didn't catch the price"). Never invent a price.
- If a photo was attached, send it to the model too so the description can use what is visible.
- On success the drop goes **live immediately**. Any previous live drop in the show is closed. Target: under 3 seconds from trigger word to live.

### 6.2 Seller assistant (core, small)

A chat box on the seller console where the seller asks things in plain language during or after the show: "how much have I sold?", "who bought the blue sweater?", "refund Maria's order". Implement with LLM tool calling over a small set of server functions: `list_orders`, `get_show_stats`, `refund_order` (asks for confirmation in the UI before executing), `close_current_drop`.

### 6.3 Show recap (core)

When the show ends, generate: total sold, units, a per-item table, sell-through speed (time from drop to sold out), and a ready-to-send confirmation message for each buyer asking for shipping details. Numbers come from the database; the LLM writes the text around them and must not alter figures.

## 7. Data and realtime

### 7.1 Tables

- `shows`: id (unguessable), slug, title, language, status (`live`/`ended`), created_at, ended_at.
- `drops`: id, show_id, title, description, attributes_json, price_cents, currency, quantity, units_left, image_path, status (`live`/`sold_out`/`closed`), source_text, created_at, sold_out_at.
- `attempts`: id, drop_id, paypal_order_id (unique), authorization_id, capture_id, payer_name, payer_email, status (`created`/`won`/`lost`/`capture_failed`/`abandoned`), created_at, resolved_at.
- `orders`: id, drop_id, attempt_id, buyer_name, buyer_email, amount_cents, currency, capture_id, status (`paid`/`refunded`), created_at.
- `paypal_log`: id, method, path, status_code, debug_id, created_at.

Money is stored as integer cents.

### 7.2 Events (SSE)

- `GET /api/shows/{slug}/events` (buyers): `drop_live`, `units_changed`, `drop_sold_out`, `drop_closed`, `show_ended`.
- `GET /api/seller/{show_id}/events` (seller): the above plus `order_won`, `attempt_lost`, `paypal_log`.

### 7.3 Privacy

Buyer pages never expose other buyers' names or emails. The public feed may show "Someone just got it".

### 7.4 Concurrency

In-memory pub/sub means **one Uvicorn worker**. Say so in the README. The atomic `UPDATE` is what guarantees correctness, not the worker count.

## 8. Configuration

`.env.example`:

```
PAYPAL_CLIENT_ID=
PAYPAL_CLIENT_SECRET=
PAYPAL_BASE_URL=https://api-m.sandbox.paypal.com
ANTHROPIC_API_KEY=
LLM_MODEL=claude-haiku-5-5
BASE_URL=http://localhost:8000
DEFAULT_CURRENCY=USD
TRIGGER_WORDS=now,ahora
DATABASE_PATH=./data/nowdrop.db
```

If PayPal or Anthropic credentials are missing, the app must start and show a clear setup message instead of crashing.

## 9. Milestones

**M1 — Skeleton and data.** Project layout, config, database, create show, seller console and buyer page rendering, create a drop from a manual form.
*Done when:* a drop created in the seller console appears on the buyer page.

**M2 — PayPal race.** Section 5 complete, with a `PayPalClient` interface, a real implementation and a fake one.
*Done when:* (a) a test fires 20 concurrent claims at a drop with quantity 3 using the fake client and gets exactly 3 `won`, 17 `lost`, `units_left == 0`, and 17 voids recorded; (b) a test proves a capture failure returns the unit; (c) a test proves claim idempotency; (d) a manual sandbox run with two buyer accounts in two browsers shows one winner and one "not charged".

**M3 — Realtime.** SSE for buyers and seller; live counter, state switches, order feed, PayPal activity panel.
*Done when:* no page needs a reload during a full drop.

**M4 — Voice and AI drop.** Section 6.1 with mic, trigger word, text fallback, photo, missing-price handling.
*Done when:* saying "alpaca sweater size M, fifteen dollars, I have three, now" produces a correct live drop; tests cover extraction with the fake LLM plus a small set of real-LLM cases marked as integration tests.

**M5 — Assistant, recap, refund.** Sections 6.2, 6.3 and the refund button.
*Done when:* the assistant answers sales questions correctly and a refund shows as completed in the sandbox.

**M6 — Polish and ship.**
- Visual design pass: one consistent look, mobile-first buyer page, clear win/lose screens, no default browser styling.
- `Dockerfile` and `render.yaml` for a one-service deploy on Render.
- `README.md` (section 11), `LICENSE` (MIT), `docs/DEMO_SCRIPT.md` (section 12).
- Seed command `python -m app.seed` that creates a demo show.

## 10. Stretch goals (only after M6)

1. **Next in line:** hold losers' authorizations for 60 seconds before voiding; if the winner's capture fails, the next one is captured automatically.
2. **Countdown drops:** the drop opens after a 3-second countdown so everyone starts together.
3. **PayPal Agent Toolkit / MCP** as the tool layer for the seller assistant instead of hand-written functions.
4. **Webhook verification** of `PAYMENT.CAPTURE.COMPLETED` as a second confirmation.

## 11. README must contain

- One-paragraph pitch and the problem it solves.
- A GIF or screenshot of the race.
- **How PayPal is used** (authorize → atomic claim → capture or void; refund) and **how AI is used** (speech-to-drop, assistant, recap), each in a short section.
- Architecture sketch and the tools/models used.
- Setup: prerequisites, sandbox app creation, creating two sandbox buyer accounts, `.env`, run, test.
- "Try it in 2 minutes" steps for judges, using the hosted URL and the text fallback.
- Limitations stated plainly: no TikTok integration (link/QR only), sandbox only, single worker, Chrome for voice.
- Roadmap.
- Licence.

## 12. Demo script (under 3 minutes)

Write `docs/DEMO_SCRIPT.md` with timings:

1. 0:00–0:20 — The problem: "mine!" in the comments, then the buyer disappears.
2. 0:20–1:00 — Seller speaks, says "now", the drop appears on two phones.
3. 1:00–1:40 — Both buyers pay; one wins, the other sees "you were not charged". Seller's order feed updates. Show the PayPal activity panel: authorize, capture, void.
4. 1:40–2:20 — Assistant: "how much have I sold?" and a refund. Then the recap.
5. 2:20–2:50 — What is next and who it is for.

No third-party logos or copyrighted music in the recording.

## 13. Definition of done

- All tests pass; the M2 sandbox run works end to end.
- Hosted URL works from a phone.
- A fresh clone runs following only the README.
- No secrets in the repo history.
- `LICENSE` at the repo root.
