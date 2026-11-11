"""NowDrop core: shows, drops, the PayPal payment race, refunds, stats and recap."""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import secrets
import uuid
from collections import defaultdict
from dataclasses import dataclass

from .ai import AI, AIError, AINotConfigured, Image
from .config import Settings
from .db import Database, now_iso, parse_iso
from .events import Broker
from .paypal import PayPalClient, PayPalError, PayPalNotConfigured

current_show_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_show_id", default=None)

SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}


class NowDropError(Exception):
    """An expected failure with a message that is safe to show to users."""

    def __init__(self, message: str, status_code: int = 400, **extra):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.extra = extra


def format_money(cents: int, currency: str) -> str:
    amount = f"{cents / 100:,.2f}"
    symbol = CURRENCY_SYMBOLS.get(currency)
    return f"{symbol}{amount}" if symbol else f"{amount} {currency}"


def order_number(order_id: int) -> str:
    return f"ND-{order_id:05d}"


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:24].strip("-")
    return slug or "show"


@dataclass
class DropDraft:
    title: str
    description: str
    price_cents: int
    quantity: int
    attributes: dict[str, str]
    image_path: str | None = None
    source_text: str = ""


class NowDrop:
    def __init__(self, settings: Settings, db: Database, broker: Broker, paypal: PayPalClient, ai: AI):
        self.settings = settings
        self.db = db
        self.broker = broker
        self.paypal = paypal
        self.ai = ai
        self._claim_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ------------------------------------------------------------------ logs

    def log_paypal_call(self, method: str, path: str, status_code: int, debug_id: str | None) -> None:
        show_id = current_show_id.get()
        created = now_iso()
        cur = self.db.execute(
            "INSERT INTO paypal_log (show_id, method, path, status_code, debug_id, created_at) VALUES (?,?,?,?,?,?)",
            (show_id, method, path, status_code, debug_id, created),
        )
        if show_id:
            self.broker.publish_seller(show_id, "paypal_log", {
                "id": cur.lastrowid, "method": method, "path": path, "status_code": status_code,
                "debug_id": debug_id, "created_at": created,
            })

    def paypal_log(self, show_id: str, limit: int = 30) -> list[dict]:
        return self.db.all("SELECT * FROM paypal_log WHERE show_id = ? ORDER BY id DESC LIMIT ?", (show_id, limit))

    # ----------------------------------------------------------------- shows

    def create_show(self, title: str, language: str = "en-US") -> dict:
        title = (title or "").strip()[:80] or "My live show"
        language = language if language in ("en-US", "es-419") else "en-US"
        show_id = secrets.token_urlsafe(18)
        slug = f"{slugify(title)}-{secrets.token_hex(2)}"
        self.db.execute(
            "INSERT INTO shows (id, slug, title, language, currency, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (show_id, slug, title, language, self.settings.default_currency, "live", now_iso()),
        )
        return self.get_show(show_id)

    def get_show(self, show_id: str) -> dict:
        show = self.db.one("SELECT * FROM shows WHERE id = ?", (show_id,))
        if not show:
            raise NowDropError("Show not found", 404)
        return show

    def get_show_by_slug(self, slug: str) -> dict:
        show = self.db.one("SELECT * FROM shows WHERE slug = ?", (slug,))
        if not show:
            raise NowDropError("Show not found", 404)
        return show

    def set_language(self, show: dict, language: str) -> None:
        if language in ("en-US", "es-419"):
            self.db.execute("UPDATE shows SET language = ? WHERE id = ?", (language, show["id"]))

    def end_show(self, show: dict) -> dict:
        if show["status"] == "ended":
            return show
        current_show_id.set(show["id"])
        live = self.db.one("SELECT * FROM drops WHERE show_id = ? AND status = 'live'", (show["id"],))
        with self.db.transaction() as conn:
            conn.execute("UPDATE drops SET status = 'closed' WHERE show_id = ? AND status = 'live'", (show["id"],))
            conn.execute("UPDATE shows SET status = 'ended', ended_at = ? WHERE id = ?", (now_iso(), show["id"]))
        if live:
            self.broker.publish_show(show, "drop_closed", self.public_drop(self.get_drop(live["id"])))
        self.broker.publish_show(show, "show_ended", {"show": show["slug"]})
        return self.get_show(show["id"])

    # ----------------------------------------------------------------- drops

    def get_drop(self, drop_id: int) -> dict:
        drop = self.db.one("SELECT * FROM drops WHERE id = ?", (drop_id,))
        if not drop:
            raise NowDropError("Drop not found", 404)
        return drop

    def public_drop(self, drop: dict | None) -> dict | None:
        if drop is None:
            return None
        state = drop["status"]
        if state == "live" and drop["units_left"] == 0:
            state = "sold_out"  # last units are being captured right now
        return {
            "id": drop["id"],
            "title": drop["title"],
            "description": drop["description"],
            "attributes": json.loads(drop["attributes_json"] or "{}"),
            "price_cents": drop["price_cents"],
            "price_display": format_money(drop["price_cents"], drop["currency"]),
            "currency": drop["currency"],
            "quantity": drop["quantity"],
            "units_left": drop["units_left"],
            "image_url": f"/uploads/{drop['image_path']}" if drop["image_path"] else None,
            "status": drop["status"],
            "state": state,
            "created_at": drop["created_at"],
            "sold_out_at": drop["sold_out_at"],
        }

    def current_drop(self, show_id: str) -> dict | None:
        """The drop buyers should see: the live one, else the latest if it sold out."""
        live = self.db.one("SELECT * FROM drops WHERE show_id = ? AND status = 'live' ORDER BY id DESC LIMIT 1",
                           (show_id,))
        if live:
            return live
        latest = self.db.one("SELECT * FROM drops WHERE show_id = ? ORDER BY id DESC LIMIT 1", (show_id,))
        return latest if latest and latest["status"] == "sold_out" else None

    def list_drops(self, show_id: str) -> list[dict]:
        return self.db.all("SELECT * FROM drops WHERE show_id = ? ORDER BY id", (show_id,))

    def create_drop(self, show: dict, draft: DropDraft) -> dict:
        if show["status"] != "live":
            raise NowDropError("This show has ended", 409)
        if draft.price_cents <= 0:
            raise NowDropError("The price must be greater than zero", missing=["price"])
        if not draft.title.strip():
            raise NowDropError("The drop needs a product name", missing=["product"])
        quantity = max(1, min(int(draft.quantity), 999))
        with self.db.transaction() as conn:
            closed = conn.execute(
                "SELECT id FROM drops WHERE show_id = ? AND status = 'live'", (show["id"],)
            ).fetchall()
            conn.execute("UPDATE drops SET status = 'closed' WHERE show_id = ? AND status = 'live'", (show["id"],))
            cur = conn.execute(
                """INSERT INTO drops (show_id, title, description, attributes_json, price_cents, currency,
                   quantity, units_left, image_path, status, source_text, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,'live',?,?)""",
                (show["id"], draft.title.strip()[:80], draft.description.strip()[:400],
                 json.dumps(draft.attributes, ensure_ascii=False), draft.price_cents, show["currency"],
                 quantity, quantity, draft.image_path, draft.source_text[:2000], now_iso()),
            )
            drop_id = cur.lastrowid
        # Announce the new drop first so buyers never flash the "waiting" screen.
        drop = self.get_drop(drop_id)
        self.broker.publish_show(show, "drop_live", self.public_drop(drop))
        for row in closed:
            self.broker.publish_show(show, "drop_closed", self.public_drop(self.get_drop(row["id"])))
        return drop

    def close_drop(self, show: dict, drop_id: int | None = None) -> dict | None:
        sql = "SELECT * FROM drops WHERE show_id = ? AND status = 'live'"
        params: tuple = (show["id"],)
        if drop_id is not None:
            sql += " AND id = ?"
            params += (drop_id,)
        drop = self.db.one(sql, params)
        if not drop:
            return None
        self.db.execute("UPDATE drops SET status = 'closed' WHERE id = ? AND status = 'live'", (drop["id"],))
        drop = self.get_drop(drop["id"])
        self.broker.publish_show(show, "drop_closed", self.public_drop(drop))
        return drop

    def save_image(self, data: bytes, media_type: str) -> str:
        if media_type not in SUPPORTED_IMAGE_TYPES:
            raise NowDropError("Photo must be JPEG, PNG, WebP or GIF")
        if len(data) > MAX_IMAGE_BYTES:
            raise NowDropError("Photo is too large (max 5 MB)")
        os.makedirs(self.settings.upload_dir, exist_ok=True)
        name = f"{uuid.uuid4().hex}.{media_type.split('/')[1]}"
        with open(os.path.join(self.settings.upload_dir, name), "wb") as fh:
            fh.write(data)
        return name

    def strip_trigger(self, text: str) -> str:
        words = "|".join(re.escape(w) for w in self.settings.trigger_words)
        if not words:
            return text.strip()
        return re.sub(rf"(?:[\s,.!¡?¿-]*\b(?:{words})\b[\s,.!?¡¿-]*)+$", "", text.strip(), flags=re.I).strip()

    async def drop_from_text(self, show: dict, text: str, image: Image | None = None) -> dict:
        """AI path: transcript (or typed text) -> live drop. Never publishes without a price."""
        if show["status"] != "live":
            raise NowDropError("This show has ended", 409)
        clean = self.strip_trigger(text)
        if len(clean) < 3:
            raise NowDropError("Describe the product first, then say the trigger word.", missing=["product"])
        try:
            extraction = await self.ai.extract_drop(clean[:4000], image=image, currency=show["currency"])
        except AINotConfigured:
            raise NowDropError("AI is not configured: set ANTHROPIC_API_KEY to create drops from speech. "
                               "You can still use the manual form.", 503)
        except AIError as exc:
            raise NowDropError(f"The AI could not read that: {exc}", 502)

        missing = [m for m in extraction.missing if m in ("price", "product")]
        if not extraction.is_product or not extraction.title:
            missing = sorted(set(missing) | {"product"})
        if extraction.price <= 0:
            missing = sorted(set(missing) | {"price"})
        if missing:
            human = {"price": "the price", "product": "what the product is"}
            raise NowDropError("I didn't catch " + " and ".join(human[m] for m in missing) + ".", 422,
                               missing=missing, extraction=extraction.as_dict())
        if extraction.currency and extraction.currency != show["currency"]:
            raise NowDropError(f"This show sells in {show['currency']}, but I heard {extraction.currency}.", 422,
                               missing=["currency"], extraction=extraction.as_dict())

        image_path = self.save_image(image.data, image.media_type) if image else None
        drop = self.create_drop(show, DropDraft(
            title=extraction.title,
            description=extraction.description,
            price_cents=int(round(extraction.price * 100)),
            quantity=extraction.quantity,
            attributes=extraction.attributes,
            image_path=image_path,
            source_text=clean,
        ))
        return {"drop": self.public_drop(drop), "extraction": extraction.as_dict()}

    # ------------------------------------------------------------ the race

    def get_attempt(self, paypal_order_id: str) -> dict:
        attempt = self.db.one("SELECT * FROM attempts WHERE paypal_order_id = ?", (paypal_order_id,))
        if not attempt:
            raise NowDropError("Unknown order", 404)
        return attempt

    def _show_for_drop(self, drop: dict) -> dict:
        return self.get_show(drop["show_id"])

    async def start_checkout(self, drop_id: int) -> str:
        """PayPal createOrder: amount always comes from the database."""
        drop = self.get_drop(drop_id)
        show = self._show_for_drop(drop)
        current_show_id.set(show["id"])
        if drop["status"] != "live" or drop["units_left"] <= 0:
            raise NowDropError("Sold out — someone was faster.", 409, result="sold_out")
        try:
            order_id = await self.paypal.create_order(
                amount_cents=drop["price_cents"], currency=drop["currency"],
                drop_id=drop["id"], description=drop["title"],
            )
        except PayPalNotConfigured:
            raise NowDropError("Payments are not configured on this server yet.", 503)
        except PayPalError as exc:
            raise NowDropError(f"PayPal could not start the payment: {exc}", 502)
        self.db.execute(
            "INSERT INTO attempts (drop_id, paypal_order_id, status, created_at) VALUES (?,?, 'created', ?)",
            (drop["id"], order_id, now_iso()),
        )
        return order_id

    def _result(self, attempt: dict) -> dict:
        drop = self.get_drop(attempt["drop_id"])
        status = attempt["status"]
        if status == "won":
            order = self.db.one("SELECT * FROM orders WHERE attempt_id = ?", (attempt["id"],))
            return {
                "result": "won",
                "order_id": order["id"],
                "order_number": order_number(order["id"]),
                "item": drop["title"],
                "amount_display": format_money(order["amount_cents"], order["currency"]),
                "buyer_name": order["buyer_name"],
            }
        if status == "lost":
            return {"result": "lost", "item": drop["title"],
                    "message": "Sold out — someone was faster. You were not charged."}
        if status == "capture_failed":
            return {"result": "failed", "item": drop["title"],
                    "message": "Your payment could not be completed. You were not charged."}
        if status == "abandoned":
            return {"result": "abandoned", "item": drop["title"],
                    "message": "Payment was cancelled. You were not charged."}
        return {"result": "pending", "item": drop["title"]}

    async def claim(self, paypal_order_id: str) -> dict:
        """PayPal onApprove: authorize -> atomic claim -> capture (winner) or void (loser).

        Idempotent per PayPal order id: a resolved attempt returns its stored result
        and makes no PayPal calls. The per-order lock stops duplicate concurrent calls.
        """
        async with self._claim_locks[paypal_order_id]:
            attempt = self.get_attempt(paypal_order_id)
            if attempt["status"] in ("won", "lost", "capture_failed"):
                return self._result(attempt)
            drop = self.get_drop(attempt["drop_id"])
            show = self._show_for_drop(drop)
            current_show_id.set(show["id"])
            prefix = paypal_order_id

            # 1. Authorize: reserve the buyer's funds.
            try:
                auth = await self.paypal.authorize_order(paypal_order_id, request_id=f"{prefix}-authorize")
            except PayPalError as exc:
                self.db.execute("UPDATE attempts SET status = 'abandoned', resolved_at = ? WHERE id = ?",
                                (now_iso(), attempt["id"]))
                return {"result": "failed", "item": drop["title"],
                        "message": f"PayPal did not approve the payment ({exc}). You were not charged."}
            self.db.execute(
                "UPDATE attempts SET authorization_id = ?, payer_name = ?, payer_email = ? WHERE id = ?",
                (auth.authorization_id, auth.payer_name, auth.payer_email, attempt["id"]),
            )

            # 2. Atomic claim: exactly one row update per available unit.
            with self.db.transaction() as conn:
                won = conn.execute(
                    "UPDATE drops SET units_left = units_left - 1 WHERE id = ? AND status = 'live' AND units_left > 0",
                    (drop["id"],),
                ).rowcount == 1

            if won:
                return await self._capture_winner(show, drop, attempt, auth.authorization_id,
                                                  auth.payer_name, auth.payer_email)
            return await self._void_loser(show, drop, attempt, auth.authorization_id, auth.payer_name)

    async def _capture_winner(self, show, drop, attempt, auth_id, payer_name, payer_email) -> dict:
        try:
            capture_id = await self.paypal.capture_authorization(
                auth_id, request_id=f"{attempt['paypal_order_id']}-capture")
        except PayPalError:
            with self.db.transaction() as conn:
                conn.execute(
                    """UPDATE drops SET units_left = units_left + 1,
                       status = CASE WHEN status IN ('live', 'sold_out') THEN 'live' ELSE status END,
                       sold_out_at = CASE WHEN status IN ('live', 'sold_out') THEN NULL ELSE sold_out_at END
                       WHERE id = ?""",
                    (drop["id"],),
                )
                conn.execute("UPDATE attempts SET status = 'capture_failed', resolved_at = ? WHERE id = ?",
                             (now_iso(), attempt["id"]))
            try:  # release the hold on the buyer's funds right away
                await self.paypal.void_authorization(auth_id, request_id=f"{attempt['paypal_order_id']}-void")
            except PayPalError:
                pass
            fresh = self.get_drop(drop["id"])
            self.broker.publish_show(show, "units_changed", {"drop_id": fresh["id"], "units_left": fresh["units_left"],
                                                             "state": self.public_drop(fresh)["state"]})
            return self._result(self.get_attempt(attempt["paypal_order_id"]))

        sold_out = False
        with self.db.transaction() as conn:
            conn.execute("UPDATE attempts SET status = 'won', capture_id = ?, resolved_at = ? WHERE id = ?",
                         (capture_id, now_iso(), attempt["id"]))
            cur = conn.execute(
                """INSERT INTO orders (drop_id, attempt_id, buyer_name, buyer_email, amount_cents, currency,
                   capture_id, status, created_at) VALUES (?,?,?,?,?,?,?, 'paid', ?)""",
                (drop["id"], attempt["id"], payer_name, payer_email, drop["price_cents"], drop["currency"],
                 capture_id, now_iso()),
            )
            order_id = cur.lastrowid
            sold_out = conn.execute(
                "UPDATE drops SET status = 'sold_out', sold_out_at = ? WHERE id = ? AND units_left = 0 AND status = 'live'",
                (now_iso(), drop["id"]),
            ).rowcount == 1

        fresh = self.get_drop(drop["id"])
        self.broker.publish_show(show, "units_changed", {
            "drop_id": fresh["id"], "units_left": fresh["units_left"],
            "state": self.public_drop(fresh)["state"], "someone_bought": True,
        })
        self.broker.publish_seller(show["id"], "order_won", self.order_view(self.get_order(order_id)))
        if sold_out:
            self.broker.publish_show(show, "drop_sold_out", self.public_drop(fresh))
        return self._result(self.get_attempt(attempt["paypal_order_id"]))

    async def _void_loser(self, show, drop, attempt, auth_id, payer_name) -> dict:
        try:
            await self.paypal.void_authorization(auth_id, request_id=f"{attempt['paypal_order_id']}-void")
        except PayPalError:
            pass  # an unvoided authorization simply expires; the buyer is never captured
        self.db.execute("UPDATE attempts SET status = 'lost', resolved_at = ? WHERE id = ?",
                        (now_iso(), attempt["id"]))
        self.broker.publish_seller(show["id"], "attempt_lost", {
            "drop_id": drop["id"], "item": drop["title"], "payer_name": payer_name,
        })
        return self._result(self.get_attempt(attempt["paypal_order_id"]))

    def abandon(self, paypal_order_id: str) -> dict:
        attempt = self.get_attempt(paypal_order_id)
        if attempt["status"] == "created":
            self.db.execute("UPDATE attempts SET status = 'abandoned', resolved_at = ? WHERE id = ? AND status = 'created'",
                            (now_iso(), attempt["id"]))
        return self._result(self.get_attempt(paypal_order_id))

    # --------------------------------------------------------------- orders

    def get_order(self, order_id: int) -> dict:
        order = self.db.one(
            """SELECT o.*, d.title AS item, d.attributes_json, d.show_id FROM orders o
               JOIN drops d ON d.id = o.drop_id WHERE o.id = ?""", (order_id,))
        if not order:
            raise NowDropError("Order not found", 404)
        return order

    def order_view(self, order: dict) -> dict:
        return {
            "id": order["id"],
            "order_number": order_number(order["id"]),
            "buyer_name": order["buyer_name"],
            "buyer_email": order["buyer_email"],
            "item": order["item"],
            "attributes": json.loads(order.get("attributes_json") or "{}"),
            "drop_id": order["drop_id"],
            "amount_cents": order["amount_cents"],
            "amount_display": format_money(order["amount_cents"], order["currency"]),
            "currency": order["currency"],
            "status": order["status"],
            "created_at": order["created_at"],
        }

    def list_orders(self, show_id: str, search: str | None = None) -> list[dict]:
        rows = self.db.all(
            """SELECT o.*, d.title AS item, d.attributes_json, d.show_id FROM orders o
               JOIN drops d ON d.id = o.drop_id WHERE d.show_id = ? ORDER BY o.id DESC""", (show_id,))
        orders = [self.order_view(r) for r in rows]
        if search:
            needle = search.lower().strip()
            terms = [t for t in re.split(r"\s+", needle) if t]
            def haystack(o: dict) -> str:
                return " ".join([o["buyer_name"], o["item"], *map(str, o["attributes"].values())]).lower()
            orders = [o for o in orders if all(t in haystack(o) for t in terms)]
        return orders

    async def refund(self, show: dict, order_id: int) -> dict:
        order = self.get_order(order_id)
        if order["show_id"] != show["id"]:
            raise NowDropError("Order not found", 404)
        if order["status"] == "refunded":
            return self.order_view(order)
        current_show_id.set(show["id"])
        try:
            refund_id = await self.paypal.refund_capture(order["capture_id"], request_id=f"refund-{order['capture_id']}")
        except PayPalNotConfigured:
            raise NowDropError("Payments are not configured on this server yet.", 503)
        except PayPalError as exc:
            raise NowDropError(f"PayPal refused the refund: {exc}", 502)
        self.db.execute("UPDATE orders SET status = 'refunded', refund_id = ? WHERE id = ?", (refund_id, order_id))
        view = self.order_view(self.get_order(order_id))
        self.broker.publish_seller(show["id"], "order_refunded", view)
        return view

    # ---------------------------------------------------------------- stats

    def stats(self, show: dict) -> dict:
        currency = show["currency"]
        drops = self.list_drops(show["id"])
        orders = self.list_orders(show["id"])
        paid = [o for o in orders if o["status"] == "paid"]
        per_drop = []
        for d in drops:
            sold = sum(1 for o in paid if o["drop_id"] == d["id"])
            revenue = sum(o["amount_cents"] for o in paid if o["drop_id"] == d["id"])
            seconds = None
            if d["sold_out_at"]:
                seconds = round((parse_iso(d["sold_out_at"]) - parse_iso(d["created_at"])).total_seconds(), 1)
            per_drop.append({
                "drop_id": d["id"], "title": d["title"],
                "price_display": format_money(d["price_cents"], d["currency"]),
                "quantity": d["quantity"], "units_sold": sold, "units_left": d["units_left"],
                "revenue_cents": revenue, "revenue_display": format_money(revenue, d["currency"]),
                "status": d["status"], "seconds_to_sell_out": seconds,
            })
        revenue = sum(o["amount_cents"] for o in paid)
        attempts = self.db.one(
            """SELECT SUM(a.status = 'won') AS won, SUM(a.status = 'lost') AS lost FROM attempts a
               JOIN drops d ON d.id = a.drop_id WHERE d.show_id = ?""", (show["id"],)) or {}
        return {
            "currency": currency,
            "revenue_cents": revenue,
            "revenue_display": format_money(revenue, currency),
            "units_sold": len(paid),
            "orders": len(paid),
            "refunded_orders": sum(1 for o in orders if o["status"] == "refunded"),
            "refunded_display": format_money(sum(o["amount_cents"] for o in orders if o["status"] == "refunded"),
                                             currency),
            "drops": len(drops),
            "buyers_too_late": attempts.get("lost") or 0,
            "per_drop": per_drop,
        }

    # ------------------------------------------------------------ assistant

    async def assistant(self, show: dict, history: list[dict]) -> dict:
        pending: list[dict] = []

        async def call_tool(name: str, args: dict) -> dict:
            if name == "list_orders":
                orders = self.list_orders(show["id"], args.get("search") or None)
                return {"orders": [{k: o[k] for k in ("id", "order_number", "buyer_name", "item", "attributes",
                                                       "amount_display", "status", "created_at")} for o in orders]}
            if name == "get_show_stats":
                return self.stats(show)
            if name == "refund_order":
                order = self.get_order(int(args["order_id"]))
                if order["show_id"] != show["id"]:
                    return {"error": "No such order in this show"}
                view = self.order_view(order)
                if view["status"] == "refunded":
                    return {"error": "This order is already refunded"}
                pending.append({"type": "refund", "order": view})
                return {"status": "awaiting_seller_confirmation", "order": view["order_number"]}
            if name == "close_current_drop":
                drop = self.close_drop(show)
                return {"closed": bool(drop), "title": drop["title"] if drop else None}
            return {"error": f"Unknown tool {name}"}

        clean = [{"role": m["role"], "content": str(m["content"])[:2000]}
                 for m in history[-12:] if m.get("role") in ("user", "assistant") and m.get("content")]
        while clean and clean[0]["role"] != "user":
            clean.pop(0)
        if not clean or clean[-1]["role"] != "user":
            raise NowDropError("Ask me something")
        context = f"Show: {show['title']!r}. Currency: {show['currency']}. Show status: {show['status']}."
        try:
            result = await self.ai.assistant(clean, context=context, call_tool=call_tool)
        except AINotConfigured:
            raise NowDropError("AI is not configured: set ANTHROPIC_API_KEY to use the assistant.", 503)
        except AIError as exc:
            raise NowDropError(str(exc), 502)
        return {"reply": result.reply, "actions": pending, "tools_used": [c["name"] for c in result.tool_calls]}

    # ---------------------------------------------------------------- recap

    def recap_facts(self, show: dict) -> dict:
        stats = self.stats(show)
        paid = [o for o in self.list_orders(show["id"]) if o["status"] == "paid"]
        return {
            "show_title": show["title"],
            "language": "es" if show["language"].startswith("es") else "en",
            "currency": show["currency"],
            "revenue_display": stats["revenue_display"],
            "units_sold": stats["units_sold"],
            "orders_count": stats["orders"],
            "refunded_orders": stats["refunded_orders"],
            "buyers_too_late": stats["buyers_too_late"],
            "drops": [{k: d[k] for k in ("title", "price_display", "quantity", "units_sold", "revenue_display",
                                         "seconds_to_sell_out")} for d in stats["per_drop"]],
            "orders": [{
                "order_id": o["id"], "order_number": o["order_number"],
                "buyer_name": o["buyer_name"], "buyer_first_name": o["buyer_name"].split()[0],
                "item": o["item"], "attributes": o["attributes"], "amount_display": o["amount_display"],
            } for o in reversed(paid)],
        }

    @staticmethod
    def _numbers(text: str) -> set[float]:
        """Numeric values in a text, so "$15.00" and "15" compare equal."""
        return {float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}

    def _fallback_message(self, o: dict, language: str) -> str:
        if language == "es":
            return (f"¡Hola {o['buyer_first_name']}! Gracias por tu compra de {o['item']} ({o['amount_display']}), "
                    f"pedido {o['order_number']}. Por favor envíame tu dirección completa de envío y un teléfono.")
        return (f"Hi {o['buyer_first_name']}! Thanks for buying {o['item']} ({o['amount_display']}), "
                f"order {o['order_number']}. Please send me your full shipping address and a phone number.")

    async def build_recap(self, show: dict, force: bool = False) -> dict:
        if show.get("recap_json") and not force:
            return json.loads(show["recap_json"])
        facts = self.recap_facts(show)
        allowed_all = self._numbers(json.dumps(facts, ensure_ascii=False))
        ai_used = True
        try:
            written = await self.ai.write_recap(facts)
        except AIError:
            written, ai_used = {"summary": "", "buyer_messages": []}, False

        summary = str(written.get("summary") or "").strip()
        if not summary or not self._numbers(summary) <= allowed_all:
            summary = (f"{facts['units_sold']} units sold for {facts['revenue_display']} "
                       f"across {len(facts['drops'])} drops.")
        by_id = {m.get("order_id"): str(m.get("message") or "") for m in written.get("buyer_messages") or []}
        messages = []
        for o in facts["orders"]:
            text = by_id.get(o["order_id"], "").strip()
            allowed = self._numbers(json.dumps(o, ensure_ascii=False))
            checked = bool(text) and self._numbers(text) <= allowed
            messages.append({"order_id": o["order_id"], "order_number": o["order_number"],
                             "buyer_name": o["buyer_name"], "item": o["item"],
                             "amount_display": o["amount_display"],
                             "message": text if checked else self._fallback_message(o, facts["language"]),
                             "ai_written": checked})
        recap = {"summary": summary, "messages": messages, "ai_used": ai_used, "generated_at": now_iso()}
        self.db.execute("UPDATE shows SET recap_json = ? WHERE id = ?", (json.dumps(recap, ensure_ascii=False), show["id"]))
        return recap
