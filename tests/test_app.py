"""M1, M3, M4, M5: pages, drops, realtime events, AI drop, assistant and recap."""

import asyncio

from app.services import DropDraft


# ----------------------------------------------------------------- M1 pages

async def test_landing_and_create_show(client):
    assert (await client.get("/")).status_code == 200
    res = await client.post("/shows", data={"title": "Friday drop", "language": "es-419"})
    assert res.status_code == 303
    seller = await client.get(res.headers["location"])
    assert seller.status_code == 200
    assert "Friday drop" in seller.text and "<svg" in seller.text  # QR code


async def test_unknown_show_is_404(client):
    assert (await client.get("/s/nope")).status_code == 404
    assert (await client.get("/seller/nope")).status_code == 404


async def test_manual_drop_appears_on_buyer_state(client, show):
    res = await client.post(f"/api/shows/{show['id']}/drops", json={"title": "Silver ring", "price": 12.5, "quantity": 2})
    assert res.status_code == 200
    state = (await client.get(f"/api/shows/{show['slug']}/state")).json()
    assert state["drop"]["title"] == "Silver ring"
    assert state["drop"]["price_display"] == "$12.50"
    assert state["drop"]["units_left"] == 2
    page = await client.get(f"/s/{show['slug']}")
    assert page.status_code == 200 and "paypal.com/sdk/js" in page.text and "intent=authorize" in page.text


async def test_new_drop_closes_previous(client, core, show):
    first = core.create_drop(show, DropDraft("A", "", 100, 1, {}))
    second = core.create_drop(show, DropDraft("B", "", 200, 1, {}))
    assert core.get_drop(first["id"])["status"] == "closed"
    assert core.current_drop(show["id"])["id"] == second["id"]


async def test_buyer_state_hides_buyer_identities(client, core, show):
    drop = core.create_drop(show, DropDraft("A", "", 100, 1, {}))
    oid = (await client.post(f"/api/drops/{drop['id']}/orders")).json()["id"]
    await client.post(f"/api/orders/{oid}/claim")
    text = (await client.get(f"/api/shows/{show['slug']}/state")).text
    assert "Buyer" not in text and "@example.com" not in text


async def test_end_show(client, core, show):
    core.create_drop(show, DropDraft("A", "", 100, 1, {}))
    res = await client.post(f"/api/shows/{show['id']}/end")
    assert res.json()["show"]["status"] == "ended"
    state = (await client.get(f"/api/shows/{show['slug']}/state")).json()
    assert state["show"]["status"] == "ended" and state["drop"] is None
    assert (await client.post(f"/api/shows/{show['id']}/drops", json={"title": "x", "price": 1})).status_code == 409


async def test_missing_credentials_do_not_crash(tmp_path):
    import httpx
    from app.config import Settings
    from app.main import create_app

    app = create_app(Settings(database_path=str(tmp_path / "x.db")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        res = await c.post("/shows", data={"title": "x"})
        page = await c.get(res.headers["location"])
        assert "PayPal is not configured" in page.text and "AI is not configured" in page.text
        show_id = res.headers["location"].rsplit("/", 1)[1]
        drop = (await c.post(f"/api/shows/{show_id}/drops", json={"title": "x", "price": 1})).json()["drop"]
        res = await c.post(f"/api/drops/{drop['id']}/orders")
        assert res.status_code == 503 and "not configured" in res.json()["error"]
        res = await c.post(f"/api/shows/{show_id}/drops/from-speech", data={"text": "hat ten dollars now"})
        assert res.status_code == 503


# --------------------------------------------------------------- M3 events

async def test_events_reach_buyers_and_seller(core, show):
    buyer = core.broker.stream(f"show:{show['slug']}")
    seller = core.broker.stream(f"seller:{show['id']}")
    assert (await buyer.__anext__()).startswith(": connected")
    assert (await seller.__anext__()).startswith(": connected")

    core.create_drop(show, DropDraft("Hat", "", 1000, 1, {}))
    assert "event: drop_live" in await asyncio.wait_for(buyer.__anext__(), 1)
    assert "event: drop_live" in await asyncio.wait_for(seller.__anext__(), 1)

    core.broker.publish_seller(show["id"], "order_won", {"buyer_name": "Ana"})
    assert "order_won" in await asyncio.wait_for(seller.__anext__(), 1)
    # buyers never get seller-only events
    core.broker.publish_show(show, "units_changed", {"units_left": 0})
    assert "units_changed" in await asyncio.wait_for(buyer.__anext__(), 1)
    await buyer.aclose()
    await seller.aclose()


async def test_win_publishes_events(client, core, show):
    seller = core.broker.stream(f"seller:{show['id']}")
    await seller.__anext__()
    drop = core.create_drop(show, DropDraft("Hat", "", 1000, 1, {}))
    oid = (await client.post(f"/api/drops/{drop['id']}/orders")).json()["id"]
    await client.post(f"/api/orders/{oid}/claim")
    events = []
    while True:
        try:
            chunk = await asyncio.wait_for(seller.__anext__(), 0.2)
        except asyncio.TimeoutError:
            break
        events.append(chunk.split("\n", 1)[0].removeprefix("event: "))
    for name in ("drop_live", "paypal_log", "units_changed", "order_won", "drop_sold_out"):
        assert name in events
    await seller.aclose()


# ------------------------------------------------------------ M4 AI drops

async def test_speech_to_drop(client, core, show, ai):
    res = await client.post(f"/api/shows/{show['id']}/drops/from-speech",
                            data={"text": "Alpaca sweater size M, fifteen dollars, I have three, now"})
    assert res.status_code == 200, res.text
    drop = res.json()["drop"]
    assert drop["title"] == "Alpaca Sweater"
    assert drop["price_cents"] == 1500
    assert drop["quantity"] == 3 and drop["units_left"] == 3
    assert drop["attributes"] == {"size": "M"}
    assert drop["state"] == "live"
    assert ai.extract_calls[-1].endswith("I have three")  # trigger word stripped


async def test_missing_price_is_not_published(client, core, show):
    res = await client.post(f"/api/shows/{show['id']}/drops/from-speech",
                            data={"text": "beautiful handmade scarf, I have two, now"})
    assert res.status_code == 422
    assert res.json()["missing"] == ["price"]
    assert "price" in res.json()["error"]
    assert core.current_drop(show["id"]) is None


async def test_photo_is_sent_and_saved(client, show, ai):
    png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15"
           b"\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")
    res = await client.post(f"/api/shows/{show['id']}/drops/from-speech",
                            data={"text": "Red cap, $9, now"}, files={"photo": ("cap.png", png, "image/png")})
    assert res.status_code == 200, res.text
    assert ai.images_seen == 1
    url = res.json()["drop"]["image_url"]
    assert (await client.get(url)).content == png


async def test_trigger_stripping(core):
    assert core.strip_trigger("red hat ten dollars now") == "red hat ten dollars"
    assert core.strip_trigger("gorra roja, diez dólares, ¡ahora!") == "gorra roja, diez dólares"
    assert core.strip_trigger("right now I have hats") == "right now I have hats"


# ------------------------------------------------- M5 assistant and recap

async def _sell(client, core, show, title, price_cents, buyers):
    drop = core.create_drop(show, DropDraft(title, "", price_cents, buyers, {}))
    ids = []
    for _ in range(buyers):
        oid = (await client.post(f"/api/drops/{drop['id']}/orders")).json()["id"]
        ids.append((await client.post(f"/api/orders/{oid}/claim")).json()["order_id"])
    return ids


async def test_assistant_answers_sales_questions(client, core, show):
    await _sell(client, core, show, "Blue sweater", 2000, 2)
    res = await client.post(f"/api/shows/{show['id']}/assistant",
                            json={"messages": [{"role": "user", "content": "How much have I sold?"}]})
    body = res.json()
    assert "$40.00" in body["reply"] and "2 units" in body["reply"]
    assert body["tools_used"] == ["get_show_stats"]


async def test_assistant_refund_needs_confirmation(client, core, show, paypal):
    await _sell(client, core, show, "Blue sweater", 2000, 2)
    target = core.list_orders(show["id"])[0]["buyer_name"]  # e.g. "Buyer 5"
    res = await client.post(f"/api/shows/{show['id']}/assistant",
                            json={"messages": [{"role": "user", "content": f"refund {target}"}]})
    body = res.json()
    assert body["actions"][0]["type"] == "refund"
    assert paypal.count("refund") == 0  # nothing refunded until the seller confirms
    assert all(o["status"] == "paid" for o in core.list_orders(show["id"]))


async def test_assistant_tools_filter_orders(core, show, client):
    await _sell(client, core, show, "Blue sweater", 2000, 1)
    await _sell(client, core, show, "Red scarf", 1000, 1)
    assert [o["item"] for o in core.list_orders(show["id"], "blue sweater")] == ["Blue sweater"]


async def test_recap_uses_database_figures(client, core, show):
    await _sell(client, core, show, "Blue sweater", 2000, 2)
    await client.post(f"/api/shows/{show['id']}/end")
    body = (await client.post(f"/api/shows/{show['id']}/recap")).json()
    assert body["stats"]["revenue_display"] == "$40.00"
    assert body["stats"]["per_drop"][0]["seconds_to_sell_out"] is not None
    assert len(body["recap"]["messages"]) == 2
    assert all(m["ai_written"] for m in body["recap"]["messages"])
    page = await client.get(f"/seller/{show['id']}/recap")
    assert page.status_code == 200 and "$40.00" in page.text


async def test_recap_rejects_invented_numbers(client, core, show, ai):
    await _sell(client, core, show, "Blue sweater", 2000, 1)

    async def lying_recap(facts):
        return {"summary": "You made $9,999 today!",
                "buyer_messages": [{"order_id": facts["orders"][0]["order_id"], "message": "You paid $25.00, thanks"}]}

    ai.write_recap = lying_recap
    recap = (await client.post(f"/api/shows/{show['id']}/recap")).json()["recap"]
    assert "9,999" not in recap["summary"] and "$20.00" in recap["summary"]
    assert "$20.00" in recap["messages"][0]["message"] and not recap["messages"][0]["ai_written"]
