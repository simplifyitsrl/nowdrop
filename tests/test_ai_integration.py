"""Real-LLM extraction cases. Run with: pytest -m integration (needs ANTHROPIC_API_KEY)."""

import os

import pytest

from app.ai import AnthropicAI

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set"),
]

CASES = [
    # (speech, price, quantity, must_be_in_title)
    ("alpaca sweater size M, fifteen dollars, I have three, now", 15, 3, "sweater"),
    ("ok guys look at this, um, leather wallet, fifteen bucks, last two", 15, 2, "wallet"),
    ("this denim jacket is twenty… no, eighteen dollars, only one", 18, 1, "jacket"),
    ("bueno chicas, chompa de alpaca talla M, tengo tres a quince dólares, ahora", 15, 3, "alpaca"),
]


@pytest.fixture(scope="module")
def ai():
    return AnthropicAI(os.environ["ANTHROPIC_API_KEY"], os.environ.get("LLM_MODEL") or "claude-haiku-5-5")


@pytest.mark.parametrize("speech,price,quantity,title_word", CASES)
async def test_real_extraction(ai, speech, price, quantity, title_word):
    result = await ai.extract_drop(speech, image=None, currency="USD")
    assert result.is_product
    assert result.price == price
    assert result.quantity == quantity
    assert title_word in result.title.lower()
    assert result.currency in ("USD", "")
    assert "price" not in result.missing


async def test_real_extraction_never_invents_price(ai):
    result = await ai.extract_drop("gorgeous vintage silk scarf, I have two of these", image=None, currency="USD")
    assert result.price == 0
    assert "price" in result.missing


async def test_real_extraction_not_a_product(ai):
    result = await ai.extract_drop("hi everyone, thanks for joining, how are you all doing today", image=None,
                                   currency="USD")
    assert not result.is_product or result.price == 0
