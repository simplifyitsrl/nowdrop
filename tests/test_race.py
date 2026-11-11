"""M2: the PayPal race — authorize, atomic claim, capture or void."""

import asyncio

from app.services import DropDraft


def make_drop(core, show, quantity=3, price_cents=1500):
    return core.create_drop(show, DropDraft(title="Alpaca sweater", description="Soft.", price_cents=price_cents,
                                            quantity=quantity, attributes={"size": "M"}))


async def checkout(client, drop_id):
    res = await client.post(f"/api/drops/{drop_id}/orders")
    assert res.status_code == 200, res.text
    return res.json()["id"]


async def test_twenty_concurrent_claims_three_units(client, core, show, paypal):
    drop = make_drop(core, show, quantity=3)
    order_ids = [await checkout(client, drop["id"]) for _ in range(20)]

    responses = await asyncio.gather(*(client.post(f"/api/orders/{oid}/claim") for oid in order_ids))
    results = [r.json()["result"] for r in responses]

    assert results.count("won") == 3
    assert results.count("lost") == 17
    fresh = core.get_drop(drop["id"])
    assert fresh["units_left"] == 0
    assert fresh["status"] == "sold_out"
    assert fresh["sold_out_at"] is not None
    assert paypal.count("authorize") == 20
    assert paypal.count("capture") == 3
    assert paypal.count("void") == 17
    statuses = core.db.all("SELECT status, COUNT(*) AS n FROM attempts GROUP BY status")
    assert {r["status"]: r["n"] for r in statuses} == {"won": 3, "lost": 17}
    orders = core.list_orders(show["id"])
    assert len(orders) == 3 and all(o["amount_cents"] == 1500 for o in orders)


async def test_amount_comes_from_database(client, core, show, paypal):
    drop = make_drop(core, show, quantity=1, price_cents=1899)
    oid = await checkout(client, drop["id"])
    assert paypal.orders[oid]["amount_cents"] == 1899


async def test_capture_failure_returns_the_unit(client, core, show, paypal):
    drop = make_drop(core, show, quantity=1)
    paypal.fail_capture = True
    oid = await checkout(client, drop["id"])

    body = (await client.post(f"/api/orders/{oid}/claim")).json()

    assert body["result"] == "failed"
    assert "not charged" in body["message"]
    fresh = core.get_drop(drop["id"])
    assert fresh["units_left"] == 1
    assert fresh["status"] == "live"
    assert core.get_attempt(oid)["status"] == "capture_failed"
    assert core.list_orders(show["id"]) == []

    # The unit is really back: the next buyer can win it.
    paypal.fail_capture = False
    oid2 = await checkout(client, drop["id"])
    assert (await client.post(f"/api/orders/{oid2}/claim")).json()["result"] == "won"


async def test_claim_is_idempotent(client, core, show, paypal):
    drop = make_drop(core, show, quantity=2)
    oid = await checkout(client, drop["id"])

    first = (await client.post(f"/api/orders/{oid}/claim")).json()
    calls_after_first = len(paypal.calls)
    second = (await client.post(f"/api/orders/{oid}/claim")).json()

    assert first == second
    assert first["result"] == "won"
    assert len(paypal.calls) == calls_after_first  # no new PayPal calls
    assert core.get_drop(drop["id"])["units_left"] == 1


async def test_concurrent_duplicate_claims_authorize_once(client, core, show, paypal):
    drop = make_drop(core, show, quantity=5)
    oid = await checkout(client, drop["id"])

    responses = await asyncio.gather(*(client.post(f"/api/orders/{oid}/claim") for _ in range(5)))

    assert len({r.text for r in responses}) == 1
    assert paypal.count("authorize") == 1
    assert core.get_drop(drop["id"])["units_left"] == 4


async def test_request_ids_sent_on_authorize_capture_void(client, core, show, paypal):
    drop = make_drop(core, show, quantity=1)
    a, b = await checkout(client, drop["id"]), await checkout(client, drop["id"])
    await client.post(f"/api/orders/{a}/claim")
    await client.post(f"/api/orders/{b}/claim")
    assert f"{a}-authorize" in paypal.request_ids
    assert f"{a}-capture" in paypal.request_ids
    assert f"{b}-authorize" in paypal.request_ids
    assert f"{b}-void" in paypal.request_ids


async def test_sold_out_fails_fast_on_create_order(client, core, show):
    drop = make_drop(core, show, quantity=1)
    oid = await checkout(client, drop["id"])
    await client.post(f"/api/orders/{oid}/claim")
    res = await client.post(f"/api/drops/{drop['id']}/orders")
    assert res.status_code == 409
    assert res.json()["result"] == "sold_out"


async def test_late_buyer_on_closed_drop_is_voided(client, core, show, paypal):
    drop = make_drop(core, show, quantity=3)
    oid = await checkout(client, drop["id"])
    core.close_drop(show, drop["id"])
    body = (await client.post(f"/api/orders/{oid}/claim")).json()
    assert body["result"] == "lost"
    assert paypal.count("void") == 1 and paypal.count("capture") == 0


async def test_cancel_marks_attempt_abandoned(client, core, show, paypal):
    drop = make_drop(core, show)
    oid = await checkout(client, drop["id"])
    calls = len(paypal.calls)
    body = (await client.post(f"/api/orders/{oid}/abandon")).json()
    assert body["result"] == "abandoned"
    assert core.get_attempt(oid)["status"] == "abandoned"
    assert len(paypal.calls) == calls


async def test_authorize_decline_wastes_no_unit(client, core, show, paypal):
    drop = make_drop(core, show, quantity=1)
    oid = await checkout(client, drop["id"])
    paypal.fail_authorize = True
    body = (await client.post(f"/api/orders/{oid}/claim")).json()
    assert body["result"] == "failed"
    assert core.get_drop(drop["id"])["units_left"] == 1


async def test_paypal_calls_are_logged(client, core, show):
    drop = make_drop(core, show, quantity=1)
    oid = await checkout(client, drop["id"])
    await client.post(f"/api/orders/{oid}/claim")
    paths = [row["path"] for row in core.paypal_log(show["id"])]
    assert any(p.endswith("/authorize") for p in paths)
    assert any(p.endswith("/capture") for p in paths)
    assert "/v2/checkout/orders" in paths


async def test_refund(client, core, show, paypal):
    drop = make_drop(core, show, quantity=1)
    oid = await checkout(client, drop["id"])
    won = (await client.post(f"/api/orders/{oid}/claim")).json()

    wrong = await client.post(f"/api/orders/{won['order_id']}/refund", json={"show_id": "nope"})
    assert wrong.status_code == 404

    res = await client.post(f"/api/orders/{won['order_id']}/refund", json={"show_id": show["id"]})
    assert res.status_code == 200
    assert res.json()["order"]["status"] == "refunded"
    assert paypal.count("refund") == 1
    # refunding twice does not call PayPal again
    await client.post(f"/api/orders/{won['order_id']}/refund", json={"show_id": show["id"]})
    assert paypal.count("refund") == 1
