"""Every LLM call in NowDrop goes through this module.

`AnthropicAI` talks to Claude; `FakeAI` is a deterministic stand-in used by the
tests (and it documents the contract each method must honour).
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

import anthropic


class AIError(Exception):
    pass


class AINotConfigured(AIError):
    pass


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------

@dataclass
class Image:
    data: bytes
    media_type: str


@dataclass
class Extraction:
    is_product: bool
    title: str
    description: str
    price: float  # 0 means "not stated"
    currency: str  # "" means "not stated"
    quantity: int
    attributes: dict[str, str]
    confidence: float
    missing: list[str]

    def as_dict(self) -> dict:
        return self.__dict__.copy()


ToolHandler = Callable[[str, dict], Awaitable[dict]]


@dataclass
class AssistantResult:
    reply: str
    tool_calls: list[dict] = field(default_factory=list)


class AI(Protocol):
    async def extract_drop(self, text: str, *, image: Image | None, currency: str) -> Extraction: ...

    async def assistant(self, history: list[dict], *, context: str, call_tool: ToolHandler) -> AssistantResult: ...

    async def write_recap(self, facts: dict) -> dict: ...


ASSISTANT_TOOLS: list[dict] = [
    {
        "name": "list_orders",
        "description": (
            "List the paid and refunded orders of this live show, newest first. Each order has its id, "
            "buyer name, item title, item attributes, amount and status. Optionally filter by a "
            "case-insensitive text that must appear in the buyer name, item title or attributes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Optional filter text, e.g. a buyer name or 'blue sweater'."}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_show_stats",
        "description": (
            "Get the sales figures for this show: total revenue, units sold, number of orders, refunds, "
            "and per-drop results (title, price, units sold, units left, status, seconds to sell out)."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "refund_order",
        "description": (
            "Request a full refund of one order. This does NOT refund immediately: it shows the seller a "
            "confirmation button, and the refund runs only when they click it. Use list_orders first to "
            "find the order id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "integer", "description": "The order id from list_orders."}},
            "required": ["order_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "close_current_drop",
        "description": "Close the drop that is currently live so nobody else can buy it.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


# ---------------------------------------------------------------------------
# Claude implementation
# ---------------------------------------------------------------------------

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "is_product": {"type": "boolean"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "price": {"type": "number"},
        "currency": {"type": "string"},
        "quantity": {"type": "integer"},
        "attributes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
                "required": ["name", "value"],
                "additionalProperties": False,
            },
        },
        "confidence": {"type": "number"},
        "missing": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["is_product", "title", "description", "price", "currency", "quantity",
                 "attributes", "confidence", "missing"],
    "additionalProperties": False,
}

EXTRACTION_SYSTEM = """You turn what a live-stream seller says out loud into a product listing.

The input is a raw speech-to-text transcript (or typed text). It can contain filler words, \
repetitions, chit-chat with the audience and self-corrections such as "twenty... no, eighteen" \
(the last stated value wins). It may be in English or Spanish. A trigger word like "now" or \
"ahora" may remain at the end; ignore it.

Return:
- is_product: false if the text does not describe something for sale.
- title: short product name, max 60 characters, in the seller's language, Title Case is fine.
- description: one or two short selling sentences in the seller's language. Use only facts that \
were said or that are clearly visible in the photo, if one is attached. No invented claims.
- price: the unit price as a number exactly as the seller stated it (e.g. "fifteen bucks" -> 15, \
"quince cincuenta" -> 15.5). If no price was clearly stated, use 0 and add "price" to missing. \
NEVER guess or estimate a price.
- currency: ISO code if the seller named a currency ("dollars", "bucks", "dólares", "$" -> USD; \
"euros" -> EUR; "bolivianos" -> BOB; "soles" -> PEN; "pesos" -> leave as "" unless the country is clear). \
Use "" if no currency was named.
- quantity: how many units are for sale ("I have three", "tengo tres", "last two", "quedan 2"). \
Default 1 when not stated.
- attributes: size, colour, material, condition, etc. as name/value pairs in the seller's language.
- confidence: 0..1, how sure you are the listing is correct.
- missing: list of required facts you could not find: any of "price", "product". Empty if complete."""


RECAP_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "buyer_messages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"order_id": {"type": "integer"}, "message": {"type": "string"}},
                "required": ["order_id", "message"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "buyer_messages"],
    "additionalProperties": False,
}

RECAP_SYSTEM = """You write the after-show recap for a live-stream seller.

You get the show figures as JSON. They come from the payment database and are final: never change, \
round, recompute or invent any number, name or item. Copy amounts exactly as given in the *_display fields.

Write:
- summary: 2-4 friendly sentences for the seller about how the show went (best seller, fastest \
sell-out, anything notable). Write in the show language ("en" English, "es" Spanish).
- buyer_messages: one message per paid order, ready to paste into a DM/WhatsApp. Thank the buyer by \
first name, confirm the item and the amount paid, and ask for their full shipping address and a \
phone number. Keep it under 70 words. Use the show language."""

ASSISTANT_SYSTEM = """You are the NowDrop seller assistant, embedded in the console of a seller who sells \
on live streams. Answer short questions about the show's sales using the tools; never guess a number, \
always read it from a tool. Keep answers to one to three short sentences. Answer in the language the \
seller writes in. Money amounts come with their currency.

To refund an order, find it with list_orders, then call refund_order. Refunds need the seller's \
click to confirm, so tell them to press the confirm button that appears. If several orders match, \
ask which one."""


class AnthropicAI:
    def __init__(self, api_key: str, model: str, client: anthropic.AsyncAnthropic | None = None):
        self.model = model
        self.client = client or (anthropic.AsyncAnthropic(api_key=api_key, timeout=30.0) if api_key else None)

    def _require(self) -> anthropic.AsyncAnthropic:
        if self.client is None:
            raise AINotConfigured("ANTHROPIC_API_KEY is not set")
        return self.client

    async def _json(self, *, system: str, content: Any, schema: dict, max_tokens: int, effort: str) -> dict:
        client = self._require()
        try:
            response = await client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": content}],
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            )
        except anthropic.APIStatusError as exc:
            raise AIError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise AIError("Could not reach the Claude API") from exc
        if response.stop_reason == "refusal":
            raise AIError("The model declined this request")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except ValueError as exc:
            raise AIError("The model returned malformed JSON") from exc

    async def extract_drop(self, text: str, *, image: Image | None, currency: str) -> Extraction:
        content: list[dict] = []
        if image is not None:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": image.media_type,
                           "data": base64.standard_b64encode(image.data).decode()},
            })
        content.append({
            "type": "text",
            "text": f"The shop's currency is {currency}.\n\nTranscript:\n<transcript>\n{text}\n</transcript>",
        })
        data = await self._json(system=EXTRACTION_SYSTEM, content=content, schema=EXTRACTION_SCHEMA,
                                max_tokens=2000, effort="low")
        return Extraction(
            is_product=bool(data.get("is_product")),
            title=str(data.get("title") or "").strip(),
            description=str(data.get("description") or "").strip(),
            price=float(data.get("price") or 0),
            currency=str(data.get("currency") or "").strip().upper(),
            quantity=int(data.get("quantity") or 1),
            attributes={a["name"]: a["value"] for a in data.get("attributes") or [] if a.get("name")},
            confidence=float(data.get("confidence") or 0),
            missing=[str(m) for m in data.get("missing") or []],
        )

    async def assistant(self, history: list[dict], *, context: str, call_tool: ToolHandler) -> AssistantResult:
        client = self._require()
        messages: list[dict] = [dict(m) for m in history]
        tool_calls: list[dict] = []
        for _ in range(6):
            try:
                response = await client.messages.create(
                    model=self.model,
                    max_tokens=4000,
                    system=f"{ASSISTANT_SYSTEM}\n\n{context}",
                    tools=ASSISTANT_TOOLS,
                    messages=messages,
                    output_config={"effort": "low"},
                )
            except anthropic.APIStatusError as exc:
                raise AIError(f"Claude API error {exc.status_code}: {exc.message}") from exc
            except anthropic.APIConnectionError as exc:
                raise AIError("Could not reach the Claude API") from exc
            if response.stop_reason == "refusal":
                return AssistantResult("Sorry, I can't help with that.", tool_calls)
            if response.stop_reason != "tool_use":
                reply = "".join(b.text for b in response.content if b.type == "text").strip()
                return AssistantResult(reply or "Done.", tool_calls)
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                try:
                    output = await call_tool(block.name, dict(block.input or {}))
                    is_error = False
                except Exception as exc:  # report tool failures back to the model
                    output, is_error = {"error": str(exc)}, True
                tool_calls.append({"name": block.name, "input": dict(block.input or {}), "output": output})
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(output, default=str), "is_error": is_error})
            messages.append({"role": "user", "content": results})
        return AssistantResult("I couldn't finish that, please try again.", tool_calls)

    async def write_recap(self, facts: dict) -> dict:
        return await self._json(system=RECAP_SYSTEM, content=json.dumps(facts, default=str),
                                schema=RECAP_SCHEMA, max_tokens=8000, effort="low")


# ---------------------------------------------------------------------------
# Deterministic fake
# ---------------------------------------------------------------------------

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
    "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15,
    "dieciseis": 16, "dieciséis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20,
    "treinta": 30, "cuarenta": 40, "cincuenta": 50,
}
_NUM = r"(\d+(?:[.,]\d{1,2})?|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"
_MONEY = r"(?:dollars?|bucks|usd|d[oó]lares|bs|bolivianos)"


def _to_number(token: str) -> float:
    token = token.lower()
    if token in _NUMBER_WORDS:
        return float(_NUMBER_WORDS[token])
    return float(token.replace(",", "."))


class FakeAI:
    """Rule-based stand-in for tests. Same contract as AnthropicAI, no network."""

    def __init__(self) -> None:
        self.extract_calls: list[str] = []
        self.images_seen = 0

    async def extract_drop(self, text: str, *, image: Image | None, currency: str) -> Extraction:
        self.extract_calls.append(text)
        if image is not None:
            self.images_seen += 1
        lower = text.lower()
        prices =[p for m in re.finditer(r"\$\s*" + _NUM + r"|\b" + _NUM + r"\s*" + _MONEY + r"\b", lower)
                  for p in [m.group(1) or m.group(2)] if p]
        price = _to_number(prices[-1]) if prices else 0.0
        qty_match = re.search(r"(?:i have|tengo|last|[uú]ltim[oa]s|quedan)\s+" + _NUM, lower)
        quantity = int(_to_number(qty_match.group(1))) if qty_match else 1
        size = re.search(r"\bsize\s+(xs|s|m|l|xl)\b|\btalla\s+(xs|s|m|l|xl)\b", lower)
        attributes = {"size": (size.group(1) or size.group(2)).upper()} if size else {}
        head = re.split(r"[,.]", text, maxsplit=1)[0]
        head = re.sub(r"(?i)\b(size|talla)\s+\w+\b", "", head)
        head = re.sub(r"(?i)^\s*(ok(ay)?|so|um+|uh+|bueno|este|look|mira)[\s,]+", "", head).strip()
        is_product = bool(head) and not lower.strip().startswith("hello")
        missing = [] if price else ["price"]
        if not is_product:
            missing.append("product")
        return Extraction(
            is_product=is_product,
            title=head[:60].strip().title() if is_product else "",
            description=f"{head.strip().capitalize()}." if is_product else "",
            price=price,
            currency="USD" if price else "",
            quantity=max(1, quantity),
            attributes=attributes,
            confidence=0.9 if price else 0.4,
            missing=missing,
        )

    async def assistant(self, history: list[dict], *, context: str, call_tool: ToolHandler) -> AssistantResult:
        question = str(history[-1]["content"]).lower()
        calls: list[dict] = []

        async def run(name: str, args: dict) -> dict:
            out = await call_tool(name, args)
            calls.append({"name": name, "input": args, "output": out})
            return out

        if "refund" in question or "reembols" in question:
            name = question.split()[-1].strip("?.!'s").replace("'s", "")
            orders = (await run("list_orders", {"search": name}))["orders"]
            if len(orders) != 1:
                return AssistantResult(f"I found {len(orders)} matching orders. Which one?", calls)
            await run("refund_order", {"order_id": orders[0]["id"]})
            return AssistantResult(f"Ready to refund order #{orders[0]['id']}. Please confirm.", calls)
        if "who" in question or "quién" in question:
            orders = (await run("list_orders", {"search": question.split()[-1].strip("?")}))["orders"]
            names = ", ".join(o["buyer_name"] for o in orders) or "nobody yet"
            return AssistantResult(f"Bought by: {names}.", calls)
        if "close" in question or "cierra" in question:
            out = await run("close_current_drop", {})
            return AssistantResult("Closed." if out.get("closed") else "There is no live drop.", calls)
        stats = await run("get_show_stats", {})
        return AssistantResult(f"You have sold {stats['revenue_display']} in {stats['units_sold']} units.", calls)

    async def write_recap(self, facts: dict) -> dict:
        return {
            "summary": f"You sold {facts['revenue_display']} across {facts['units_sold']} units.",
            "buyer_messages": [
                {"order_id": o["order_id"],
                 "message": f"Hi {o['buyer_first_name']}! Thanks for buying {o['item']} for "
                            f"{o['amount_display']}. Please send your shipping address and phone number."}
                for o in facts["orders"]
            ],
        }
