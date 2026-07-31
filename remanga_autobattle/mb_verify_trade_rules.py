"""Verify same-rank 1→2 trade rules: no X, scheduled pattern."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(levelname)s %(name)s: %(message)s",
)

from config import load_config
from services.mangabuff_service import (
    MangaBuffService,
    TRADE_SAME_WANT_COUNT,
)


async def main() -> None:
    cfg = load_config()
    svc = MangaBuffService(
        cfg,
        user_data_dir=cfg.mangabuff_user_data_dir,
        delay_min_sec=0.02,
        delay_max_sec=0.05,
        email=cfg.mangabuff_email,
        password=cfg.mangabuff_password,
    )
    await svc.start(headless=True)
    await svc.ensure_login()
    page = svc._page
    assert page is not None
    await page.goto("https://mangabuff.ru/trades", wait_until="domcontentloaded")
    cards = await svc._tradable_inventory_cards(page)
    units = svc._build_profitable_offer_units(cards)
    sample = []
    for u in units[:12]:
        sample.append(
            {
                "kind": u["kind"],
                "offer": [c.get("rank") for c in u["offer_cards"]],
                "want": f"{u.get('want_count')}x{u.get('want_rank')}",
            }
        )
    x_offered = any(
        str(c.get("rank") or "").upper() == "X"
        for u in units
        for c in u["offer_cards"]
    )
    x_wanted = any(str(u.get("want_rank") or "").upper() == "X" for u in units)
    bad_kind = [u for u in units if u.get("kind") != "same2"]
    bad_count = [
        u for u in units if int(u.get("want_count") or 0) != TRADE_SAME_WANT_COUNT
    ]
    print(
        json.dumps(
            {
                "tradable_no_x": len(cards),
                "units": len(units),
                "sample": sample,
                "x_in_offers": x_offered,
                "x_wanted": x_wanted,
                "want_count": TRADE_SAME_WANT_COUNT,
                "bad_kind": len(bad_kind),
                "bad_count": len(bad_count),
            },
            ensure_ascii=False,
        )
    )

    # unit helpers
    assert (
        svc._is_outgoing_offer_profitable(
            [{"rank": "D"}], [{"rank": "D"}, {"rank": "D"}]
        )
        is True
    )
    assert (
        svc._is_outgoing_offer_profitable([{"rank": "D"}], [{"rank": "D"}]) is False
    )
    assert (
        svc._is_outgoing_offer_profitable(
            [{"rank": "E"}], [{"rank": "D"}, {"rank": "D"}]
        )
        is False
    )
    assert (
        svc._is_outgoing_offer_profitable(
            [{"rank": "S"}, {"rank": "S"}], [{"rank": "X"}]
        )
        is False
    )
    assert svc._is_outgoing_offer_profitable([{"rank": "X"}], [{"rank": "X"}]) is False
    assert svc._is_incoming_trade_profitable(
        [{"rank": "D"}], [{"rank": "D"}, {"rank": "D"}]
    )
    assert not svc._is_incoming_trade_profitable(
        [{"rank": "D"}], [{"rank": "D"}]
    )
    assert not svc._is_incoming_trade_profitable(
        [{"rank": "D"}, {"rank": "D"}], [{"rank": "D"}]
    )
    assert not x_offered and not x_wanted and not bad_kind and not bad_count
    print("HELPERS_OK")

    sent = await svc._run_card_trades_unlocked(offers=5, create_offers=True)
    print(
        json.dumps(
            {"sent": sent, "trades_total": svc.stats.trades_sent},
            ensure_ascii=False,
        )
    )
    await svc.stop()


if __name__ == "__main__":
    asyncio.run(main())
