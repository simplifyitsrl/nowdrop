"""Create a demo show with one live drop: `python -m app.seed`."""

from __future__ import annotations

from .config import load_settings
from .db import Database
from .events import Broker
from .services import DropDraft, NowDrop


def main() -> None:
    settings = load_settings()
    core = NowDrop(settings, Database(settings.database_path), Broker(), paypal=None, ai=None)  # type: ignore[arg-type]
    show = core.create_show("NowDrop demo show", "en-US")
    core.create_drop(show, DropDraft(
        title="Alpaca Sweater",
        description="Hand-knit baby alpaca, warm and light. Size M.",
        price_cents=1500,
        quantity=3,
        attributes={"size": "M", "colour": "oatmeal"},
        source_text="(seed)",
    ))
    base = settings.base_url or "http://localhost:8000"
    print("Demo show created.")
    print(f"  Seller console: {base}/seller/{show['id']}")
    print(f"  Buyer link:     {base}/s/{show['slug']}")


if __name__ == "__main__":
    main()
