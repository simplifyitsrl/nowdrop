"""FastAPI application: pages, JSON API and Server-Sent Events."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import segno
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .ai import AI, AnthropicAI, Image
from .config import Settings, load_settings
from .db import Database
from .events import Broker
from .paypal import PayPalClient, RealPayPalClient
from .services import DropDraft, NowDrop, NowDropError, format_money

HERE = os.path.dirname(os.path.abspath(__file__))


class ManualDrop(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    price: float = Field(gt=0, le=100000)
    quantity: int = Field(default=1, ge=1, le=999)
    description: str = Field(default="", max_length=400)


class RefundRequest(BaseModel):
    show_id: str


class AssistantRequest(BaseModel):
    messages: list[dict]


class LanguageRequest(BaseModel):
    language: str


def create_app(settings: Settings | None = None, paypal: PayPalClient | None = None,
               ai: AI | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.database_path)
    broker = Broker()
    core: NowDrop  # assigned below; the PayPal client logs through it

    def on_paypal_call(method, path, status_code, debug_id):
        core.log_paypal_call(method, path, status_code, debug_id)

    if paypal is None:
        paypal = RealPayPalClient(settings.paypal_client_id, settings.paypal_client_secret,
                                  settings.paypal_base_url, on_call=on_paypal_call)
    elif getattr(paypal, "on_call", "missing") is None:
        paypal.on_call = on_paypal_call
    ai = ai or AnthropicAI(settings.anthropic_api_key, settings.llm_model)
    core = NowDrop(settings, db, broker, paypal, ai)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if hasattr(paypal, "aclose"):
            await paypal.aclose()
        db.close()

    app = FastAPI(title="NowDrop", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.state.core = core
    app.state.settings = settings
    templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
    templates.env.globals["format_money"] = format_money
    os.makedirs(settings.upload_dir, exist_ok=True)
    app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
    app.mount("/uploads", StaticFiles(directory=settings.upload_dir), name="uploads")

    @app.exception_handler(NowDropError)
    async def nowdrop_error(_request: Request, exc: NowDropError):
        return JSONResponse({"error": exc.message, **exc.extra}, status_code=exc.status_code)

    def public_base(request: Request) -> str:
        return settings.base_url or str(request.base_url).rstrip("/")

    def page(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
        ctx.setdefault("settings", settings)
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def not_found(request: Request) -> HTMLResponse:
        return page(request, "message.html", status_code=404, title="Not found",
                    message="This show does not exist. Check the link, or start a new show.")

    # ---------------------------------------------------------------- pages

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return page(request, "index.html")

    @app.post("/shows")
    async def create_show(title: str = Form(""), language: str = Form("en-US")):
        show = core.create_show(title, language)
        return RedirectResponse(f"/seller/{show['id']}", status_code=303)

    @app.get("/seller/{show_id}", response_class=HTMLResponse)
    async def seller_console(request: Request, show_id: str):
        try:
            show = core.get_show(show_id)
        except NowDropError:
            return not_found(request)
        show_url = f"{public_base(request)}/s/{show['slug']}"
        qr_svg = segno.make(show_url, error="m").svg_inline(scale=6, dark="#14121f", light="#ffffff", border=2)
        return page(request, "seller.html", show=show, show_url=show_url, qr_svg=qr_svg)

    @app.get("/seller/{show_id}/recap", response_class=HTMLResponse)
    async def recap_page(request: Request, show_id: str):
        try:
            show = core.get_show(show_id)
        except NowDropError:
            return not_found(request)
        return page(request, "recap.html", show=show, stats=core.stats(show))

    @app.get("/s/{slug}", response_class=HTMLResponse)
    async def buyer_page(request: Request, slug: str):
        try:
            show = core.get_show_by_slug(slug)
        except NowDropError:
            return not_found(request)
        return page(request, "show.html", show=show)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "paypal": settings.paypal_configured, "ai": settings.ai_configured}

    # ------------------------------------------------------------- state API

    def public_show(show: dict) -> dict:
        return {"slug": show["slug"], "title": show["title"], "status": show["status"], "currency": show["currency"]}

    @app.get("/api/shows/{slug}/state")
    async def buyer_state(slug: str):
        show = core.get_show_by_slug(slug)
        return {"show": public_show(show), "drop": core.public_drop(core.current_drop(show["id"]))}

    @app.get("/api/seller/{show_id}/state")
    async def seller_state(show_id: str):
        show = core.get_show(show_id)
        live = core.db.one("SELECT * FROM drops WHERE show_id = ? ORDER BY id DESC LIMIT 1", (show_id,))
        return {
            "show": {**public_show(show), "language": show["language"]},
            "drop": core.public_drop(live),
            "orders": core.list_orders(show_id),
            "paypal_log": core.paypal_log(show_id),
            "stats": core.stats(show),
        }

    # ----------------------------------------------------------------- drops

    @app.post("/api/shows/{show_id}/drops")
    async def manual_drop(show_id: str, body: ManualDrop):
        show = core.get_show(show_id)
        drop = core.create_drop(show, DropDraft(
            title=body.title, description=body.description, price_cents=int(round(body.price * 100)),
            quantity=body.quantity, attributes={}, source_text="(manual form)",
        ))
        return {"drop": core.public_drop(drop)}

    @app.post("/api/shows/{show_id}/drops/from-speech")
    async def drop_from_speech(show_id: str, text: str = Form(...), photo: UploadFile | None = File(None)):
        show = core.get_show(show_id)
        image = None
        if photo is not None and photo.filename:
            data = await photo.read()
            if data:
                media_type = (photo.content_type or "").lower()
                if media_type == "image/jpg":
                    media_type = "image/jpeg"
                if media_type not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                    raise NowDropError("Photo must be JPEG, PNG, WebP or GIF")
                image = Image(data=data, media_type=media_type)
        return await core.drop_from_text(show, text, image)

    @app.post("/api/shows/{show_id}/drops/{drop_id}/close")
    async def close_drop(show_id: str, drop_id: int):
        show = core.get_show(show_id)
        drop = core.close_drop(show, drop_id)
        return {"drop": core.public_drop(drop)}

    @app.post("/api/shows/{show_id}/language")
    async def set_language(show_id: str, body: LanguageRequest):
        core.set_language(core.get_show(show_id), body.language)
        return {"ok": True}

    @app.post("/api/shows/{show_id}/end")
    async def end_show(show_id: str):
        show = core.end_show(core.get_show(show_id))
        return {"show": public_show(show), "recap_url": f"/seller/{show_id}/recap"}

    # --------------------------------------------------------------- payments

    @app.post("/api/drops/{drop_id}/orders")
    async def create_order(drop_id: int):
        return {"id": await core.start_checkout(drop_id)}

    @app.post("/api/orders/{paypal_order_id}/claim")
    async def claim(paypal_order_id: str):
        return await core.claim(paypal_order_id)

    @app.post("/api/orders/{paypal_order_id}/abandon")
    async def abandon(paypal_order_id: str):
        return core.abandon(paypal_order_id)

    @app.post("/api/orders/{order_id}/refund")
    async def refund(order_id: int, body: RefundRequest):
        show = core.get_show(body.show_id)
        return {"order": await core.refund(show, order_id)}

    # -------------------------------------------------------------------- AI

    @app.post("/api/shows/{show_id}/assistant")
    async def assistant(show_id: str, body: AssistantRequest):
        return await core.assistant(core.get_show(show_id), body.messages)

    @app.post("/api/shows/{show_id}/recap")
    async def recap(show_id: str, force: bool = False):
        show = core.get_show(show_id)
        return {"recap": await core.build_recap(show, force=force), "stats": core.stats(show)}

    # ---------------------------------------------------------------- events

    def sse(channel: str, request: Request) -> StreamingResponse:
        return StreamingResponse(
            core.broker.stream(channel, request.is_disconnected),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    @app.get("/api/shows/{slug}/events")
    async def buyer_events(slug: str, request: Request):
        show = core.get_show_by_slug(slug)
        return sse(f"show:{show['slug']}", request)

    @app.get("/api/seller/{show_id}/events")
    async def seller_events(show_id: str, request: Request):
        show = core.get_show(show_id)
        return sse(f"seller:{show['id']}", request)

    return app
