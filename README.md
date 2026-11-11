# NowDrop — say it, drop it, first paid wins

Live-stream sellers on TikTok, Instagram and Facebook Live sell by asking “who wants it?”. Buyers comment “mine!”, and then the seller spends the rest of the night chasing payments by hand — and many “winners” never pay. **NowDrop removes that.** The seller describes the product out loud and says the trigger word **“now”**. AI turns the speech into a live listing in seconds, and **the first buyer whose PayPal payment is actually secured wins**. Everyone who arrives late is never charged. The order is recorded under the winner’s name automatically.

It is not an auction: the price is fixed and speed decides. Buyers need no app and no account with us — just one permanent link pinned in the stream chat.

> Built for the PayPal AI Hackathon (Devpost). PayPal sandbox only.

![The race: two buyers, one unit — one wins, the other is not charged](docs/race.gif)

<!-- Record docs/race.gif with two sandbox buyers following docs/DEMO_SCRIPT.md step 3. -->

**Hosted demo:** `https://<your-render-service>.onrender.com` (see [Try it in 2 minutes](#try-it-in-2-minutes)).

---

## How PayPal is used

PayPal is the referee of the race, not just the checkout.

1. **Create order** — the buyer taps the PayPal button; the server creates an Orders v2 order with `intent: AUTHORIZE`. The amount is read from the database, never from the browser.
2. **Authorize** — after the buyer approves, the server calls `POST /v2/checkout/orders/{id}/authorize`. The buyer’s funds are now reserved.
3. **Atomic claim** — one SQL statement decides the winner:
   `UPDATE drops SET units_left = units_left - 1 WHERE id = ? AND status = 'live' AND units_left > 0`.
   One row updated = you won a unit.
4. **Winner → capture** (`POST /v2/payments/authorizations/{id}/capture`). The order is recorded with the payer’s name from PayPal. If the capture fails, the unit goes back on sale and the buyer is told nothing was charged.
5. **Loser → void** (`POST /v2/payments/authorizations/{id}/void`). The hold is released; no charge, no refund to chase.
6. **Refund** — one button per order (or ask the assistant) calls `POST /v2/payments/captures/{id}/refund`.

Why authorize first? The winner is the first buyer whose funds are *really* secured, so a declined card never wastes a unit, and losers get a void instead of a charge-and-refund.

Details that matter: every authorize/capture/void/refund sends a `PayPal-Request-Id`; the claim endpoint is idempotent per PayPal order id (a retry returns the stored result and makes no new PayPal calls); every PayPal call is logged with its HTTP status and `PayPal-Debug-Id` and streamed live to the **PayPal activity** panel on the seller console.

## How AI is used

All LLM calls live in [`app/ai.py`](app/ai.py) (Claude via the Anthropic SDK, model from `LLM_MODEL`, default `claude-haiku-5-5`).

- **Speech → drop (core).** The browser’s Web Speech API keeps a rolling transcript. When the seller says the trigger word (“now” / “ahora”), the text since the last drop — plus an optional photo — goes to Claude with a strict JSON schema: title, selling description in the seller’s language, price, currency, quantity, attributes (size, colour…), confidence and what is missing. It handles casual speech: filler words, “fifteen bucks, last two”, “tengo tres a quince dólares”, self-corrections like “twenty… no, eighteen”. **If the price is missing it never publishes and never guesses** — the console says “I didn’t catch the price”, and the seller just says it again.
- **Seller assistant.** A chat box that answers “how much have I sold?”, “who bought the blue sweater?”, “refund Maria’s order” using tool calling over four server functions (`list_orders`, `get_show_stats`, `refund_order`, `close_current_drop`). Refunds always need a confirmation click.
- **Show recap.** Totals, per-item table and sell-through speed come straight from the database. Claude writes the summary and a ready-to-send message per buyer asking for shipping details. Every number in the AI text is checked against the database figures; anything that does not match is replaced by a safe template.

## Architecture

```
 Seller phone/laptop                     NowDrop server (FastAPI, 1 worker)                 PayPal sandbox
 ┌────────────────────┐  text/photo   ┌───────────────────────────────────┐
 │ Seller console     │──────────────▶│ ai.py ──▶ Claude (extract drop)    │
 │ Web Speech API     │◀── SSE ───────│                                   │
 │ order feed, chat   │               │ services.py                       │  Orders v2 ┌──────────┐
 └────────────────────┘               │  create order ───────────────────────────────▶│          │
                                      │  authorize ──────────────────────────────────▶│  PayPal  │
 Buyer phones (link / QR)             │  atomic UPDATE in SQLite (WAL)    │ Payments v2│   REST   │
 ┌────────────────────┐  PayPal JS SDK│  capture (winner) / void (loser) ────────────▶│          │
 │ /s/{show}          │──────────────▶│  refund ─────────────────────────────────────▶│          │
 │ live drop, units   │◀── SSE ───────│ events.py (in-memory pub/sub)     │            └──────────┘
 └────────────────────┘               └───────────────────────────────────┘
```

- **Backend:** Python 3.11+, FastAPI, Uvicorn, SQLite (`sqlite3`, WAL), `httpx` for PayPal REST, `anthropic` SDK, `segno` for QR codes.
- **Frontend:** server-rendered Jinja2 + vanilla JS, no build step. PayPal JS SDK buttons (`intent=authorize`), Web Speech API, Server-Sent Events.
- **Models:** Claude (`claude-haiku-5-5` by default — fast enough for the “under 3 seconds” target).
- **Tests:** `pytest` + `pytest-asyncio`, with a fake PayPal client and a fake LLM.

```
app/
  main.py      routes, pages, SSE endpoints
  services.py  shows, drops, the payment race, refunds, stats, recap
  paypal.py    RealPayPalClient (REST) + FakePayPalClient (tests)
  ai.py        AnthropicAI + FakeAI (tests)
  events.py    in-memory pub/sub for SSE
  db.py        SQLite schema and access
  seed.py      `python -m app.seed` demo data
tests/         race, idempotency, capture failure, AI drop, assistant, recap
```

## Setup

### Prerequisites

- Python 3.11 or newer
- A free PayPal developer account: <https://developer.paypal.com>
- An Anthropic API key: <https://console.anthropic.com>
- Chrome (desktop or Android) for voice input; any browser works with the text box

### 1. PayPal sandbox app

1. Log in to the [PayPal Developer Dashboard](https://developer.paypal.com/dashboard/) and switch to **Sandbox**.
2. **Apps & Credentials → Create App** (type: Merchant). Copy the **Client ID** and **Secret**.

### 2. Two sandbox buyer accounts

1. Go to **Testing Tools → Sandbox Accounts → Create account**.
2. Create two **Personal (buyer)** accounts, e.g. `buyer-a@…` and `buyer-b@…` (US, with a PayPal balance).
3. Open each one (⋯ → *View/Edit account*) to see its password. You will log in with these on the buyer page.

### 3. Configure and run

```bash
git clone <this repo> nowdrop && cd nowdrop
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # then fill in PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, ANTHROPIC_API_KEY
uvicorn app.main:create_app --factory --port 8000 --workers 1
```

Open <http://localhost:8000>. Optional demo data: `python -m app.seed`.

If PayPal or Anthropic credentials are missing the app still starts and shows a clear setup message where they are needed.

| Variable | Meaning |
|---|---|
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | Sandbox REST app credentials |
| `PAYPAL_BASE_URL` | `https://api-m.sandbox.paypal.com` |
| `ANTHROPIC_API_KEY` | Claude API key |
| `LLM_MODEL` | Claude model id, default `claude-haiku-5-5` |
| `BASE_URL` | Public URL used for the show link and QR code (falls back to the request host) |
| `DEFAULT_CURRENCY` | Currency for all drops, default `USD` |
| `TRIGGER_WORDS` | Comma-separated trigger words, default `now,ahora` |
| `DATABASE_PATH` | SQLite file, default `./data/nowdrop.db` |

### 4. Test

```bash
pytest                       # 30 fast tests, no network
pytest -m integration        # real Claude extraction cases (needs ANTHROPIC_API_KEY)
```

The race test fires **20 concurrent claims at a drop with 3 units** and checks exactly 3 won, 17 lost, 17 voids and `units_left == 0`. Other tests cover capture failure (the unit comes back), idempotent and concurrent duplicate claims, request ids, refunds, AI drop extraction, missing-price handling, the assistant and the recap number check.

### 5. Deploy (Render)

The repo includes a `Dockerfile` and `render.yaml`. On Render: **New → Blueprint**, pick this repo, then fill in the secret env vars (`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `ANTHROPIC_API_KEY`, and `BASE_URL` = your `https://….onrender.com` URL). HTTPS matters: browsers only allow the microphone and camera on secure origins.

## Try it in 2 minutes

Using the hosted URL:

1. Click **Start a live show**.
2. In the console, type `Alpaca sweater size M, fifteen dollars, I have two` in the text box and press **Drop it**. (Or tap the mic in Chrome, say it, and end with “now”.)
3. Open the show link (or scan the QR) in two browsers — e.g. a normal and a private window.
4. In each, press the PayPal button and log in with a different sandbox buyer. With two units both win; drop one unit to see the race: one buyer gets “You got it!”, the other “Sold out — someone was faster. You were not charged.”
5. On the console, open **PayPal activity**: create order, authorize, capture, void.
6. Ask the assistant “how much have I sold?”, refund an order from the feed, then press **End show** for the AI recap.

Try `beautiful silk scarf, I have two` to see the AI refuse to publish without a price.

## Limitations

- **No TikTok/Instagram integration.** NowDrop runs next to the stream; buyers reach it through a pinned link or QR code.
- **PayPal sandbox only.** No real money moves.
- **Single worker.** Live events use in-memory pub/sub, so the server runs one Uvicorn worker. Winner selection does not depend on that: it is decided by the atomic SQL update.
- **Voice needs Chrome** (Web Speech API). Other browsers use the text box.
- One currency per deployment (`DEFAULT_CURRENCY`).
- No seller login: the seller console is protected by an unguessable URL.
- On Render’s free plan the SQLite file is wiped on redeploy (attach a disk at `/data` to keep it).

## Roadmap

- **Next in line:** hold losers’ authorizations for a minute and capture the next buyer automatically if a winner’s capture fails.
- **Countdown drops** so every viewer starts at the same moment.
- **PayPal Agent Toolkit / MCP** as the assistant’s tool layer.
- **Webhook confirmation** of `PAYMENT.CAPTURE.COMPLETED`.
- Shipping address capture from PayPal, seller accounts, multi-currency shows, and a chat-bot that posts each drop to the stream chat.

## Licence

[MIT](LICENSE)
