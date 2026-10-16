from decimal import Decimal
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from polyfi.config import Settings
from polyfi.server import create_app
from polyfi.streams import BookCache

H = {"X-Polyfi-Token": ""}
LOCAL = "http://127.0.0.1:8787"


@pytest.fixture
def client():
    with TestClient(create_app(Settings(mode="demo", db_path=":memory:")), base_url=LOCAL) as c:
        yield c


@pytest.fixture
def token_client():
    with TestClient(create_app(Settings(mode="demo", db_path=":memory:", dashboard_token="s3cret"))) as c:
        yield c


def running(c) -> bool:
    return c.app.state.engine.running


def test_cross_site_post_without_header_is_rejected(client):
    # What a malicious page can send without a CORS preflight: text/plain, no custom headers.
    r = client.post("/api/bot/start", content="x", headers={"Content-Type": "text/plain", "Origin": "https://evil.example"})
    assert r.status_code in (401, 403)
    r = client.post("/api/bot/start")
    assert r.status_code in (401, 403)
    assert not running(client)


def test_same_origin_with_header_is_allowed(client):
    r = client.post("/api/bot/start", headers={**H, "Origin": LOCAL})
    assert r.status_code == 200 and running(client)
    assert client.get("/api/state", headers=H).status_code == 200
    assert client.post("/api/bot/stop", headers=H).status_code == 200
    assert not running(client)


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://127.0.0.1.evil.example", "http://127.0.0.1:9999"])
def test_mismatched_origin_is_rejected(client, origin):
    r = client.post("/api/bot/start", headers={**H, "Origin": origin})
    assert r.status_code == 403
    assert not running(client)


def test_dns_rebinding_without_token_is_rejected(client):
    # A rebound attacker page is same-origin with itself: Host and Origin both say evil.example.
    h = {**H, "Host": "evil.example:8787", "Origin": "http://evil.example:8787"}
    assert client.post("/api/bot/start", headers=h).status_code == 403
    assert client.get("/api/state", headers={**H, "Host": "evil.example"}).status_code == 403
    assert not running(client)


def test_token_mode_works_behind_a_proxy_that_rewrites_host(token_client):
    # Behind the Cloudflare Worker the Host/Origin pair may not match; the token is the gate.
    h = {"X-Polyfi-Token": "s3cret", "Host": "container", "Origin": "https://polyfi-bot.example.workers.dev"}
    assert token_client.post("/api/bot/start", headers=h).status_code == 200
    assert running(token_client)


def test_token_configured(token_client):
    c = token_client
    assert c.get("/api/state", headers={"X-Polyfi-Token": "wrong"}).status_code == 401
    assert c.get("/api/state", headers=H).status_code == 401
    assert c.get("/api/state").status_code in (401, 403)
    # Non-ASCII header bytes must give 401, not a 500 from str comparison.
    r = c.get("/api/state", headers=[("X-Polyfi-Token", "pässwörd".encode("utf-8"))])
    assert r.status_code == 401
    r = c.get("/api/state", headers=[("X-Polyfi-Token", "ü".encode("latin-1"))])
    assert r.status_code == 401
    assert c.get("/api/state", headers={"X-Polyfi-Token": "s3cret"}).status_code == 200
    assert c.post("/api/bot/start", headers={"X-Polyfi-Token": "wrong"}).status_code == 401
    assert not running(c)


@pytest.mark.parametrize(
    "body",
    ['[1, 2]', '{"amount": NaN, "recipient": "0xabc"}', '{"amount": "nan"}', '{"amount": "inf"}',
     '{"amount": null}', '{"amount": "abc"}', '"str"', 'not json'],
)
def test_withdraw_bad_bodies_return_400(client, body):
    r = client.post("/api/withdraw", content=body, headers={**H, "Content-Type": "application/json"})
    assert r.status_code == 400, r.text


def test_withdraw_in_demo_mode_is_400(client):
    r = client.post("/api/withdraw", json={"amount": 5, "recipient": "0xabc"}, headers=H)
    assert r.status_code == 400


def test_index_and_healthz_need_no_headers(token_client):
    assert token_client.get("/").status_code == 200
    assert token_client.get("/healthz").json() == {"ok": True}


def test_price_change_one_sided_keeps_other_side():
    c = BookCache()
    c.apply(NS(type="best_bid_ask", payload=NS(asset_id="a", best_bid=Decimal("0.40"), best_ask=Decimal("0.45"))))
    c.apply(NS(type="price_change", payload=NS(price_changes=(NS(asset_id="a", best_bid=None, best_ask=Decimal("0.44")),))))
    assert (c.get("a").bid, c.get("a").ask) == (0.40, 0.44)
    c.apply(NS(type="price_change", payload=NS(price_changes=(NS(asset_id="a", best_bid=Decimal("0.41"), best_ask=None),))))
    assert (c.get("a").bid, c.get("a").ask) == (0.41, 0.44)
    # Unknown asset with one side: the other stays None.
    c.apply(NS(type="price_change", payload=NS(price_changes=(NS(asset_id="b", best_bid=Decimal("0.3"), best_ask=None),))))
    assert (c.get("b").bid, c.get("b").ask) == (0.3, None)


def test_book_snapshot_is_authoritative_for_empty_side():
    lv = lambda p: NS(price=Decimal(str(p)), size=Decimal("10"))  # noqa: E731
    c = BookCache()
    c.apply(NS(type="best_bid_ask", payload=NS(asset_id="a", best_bid=Decimal("0.40"), best_ask=Decimal("0.45"))))
    c.apply(NS(type="book", payload=NS(asset_id="a", bids=(lv(0.42),), asks=())))
    assert (c.get("a").bid, c.get("a").ask) == (0.42, None)
    c.apply(NS(type="best_bid_ask", payload=NS(asset_id="a", best_bid=None, best_ask=Decimal("0.5"))))
    assert (c.get("a").bid, c.get("a").ask) == (None, 0.5)
