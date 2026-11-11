import httpx
import pytest

from app.ai import FakeAI
from app.config import Settings
from app.main import create_app
from app.paypal import FakePayPalClient


@pytest.fixture
def settings(tmp_path):
    return Settings(
        paypal_client_id="test", paypal_client_secret="test", anthropic_api_key="",
        database_path=str(tmp_path / "test.db"), base_url="http://test",
    )


@pytest.fixture
def paypal():
    return FakePayPalClient(delay=0.01)


@pytest.fixture
def ai():
    return FakeAI()


@pytest.fixture
def app(settings, paypal, ai):
    return create_app(settings, paypal=paypal, ai=ai)


@pytest.fixture
def core(app):
    return app.state.core


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def show(core):
    return core.create_show("Test show", "en-US")
