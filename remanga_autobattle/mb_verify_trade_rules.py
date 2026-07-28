"""Verify profitable trade rules: R→R+1, 2S→1X, no X given."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(levelname)s %(name)s: %(message)s")

from config import load_config
from services.mangabuff_service import (
    MangaBuffService,
    next_higher_rank,
    TRADE_S_FOR_X,
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
                "want": u["want_rank"],
            }
        )
    x_offered = any(
        str(c.get("rank") or "").upper() == "X"
        for u in units
        for c in u["offer_cards"]
    )
    print(
        json.dumps(
            {
                "tradable_no_x": len(cards),
                "units": len(units),
                "sample": sample,
                "x_in_offers": x_offered,
                "s_for_x": TRADE_S_FOR_X,
                "e_wants": next_higher_rank("E"),
                "a_wants": next_higher_rank("A"),
            },
            ensure_ascii=False,
        )
    )
    # unit tests for profitability helpers
    assert svc._is_outgoing_offer_profitable([{"rank": "E"}], [{"rank": "D"}]) is True
    assert svc._is_outgoing_offer_profitable([{"rank": "E"}], [{"rank": "S"}]) is False
    assert (
        svc._is_outgoing_offer_profitable(
            [{"rank": "S"}, {"rank": "S"}], [{"rank": "X"}]
        )
        is True
    )
    assert svc._is_outgoing_offer_profitable([{"rank": "S"}], [{"rank": "X"}]) is False
    assert svc._is_outgoing_offer_profitable([{"rank": "X"}], [{"rank": "X"}]) is False
    assert svc._is_outgoing_offer_profitable([{"rank": "E"}], [{"rank": ""}]) is None
    assert svc._is_incoming_trade_profitable([{"rank": "E"}], [{"rank": "D"}])
    assert not svc._is_incoming_trade_profitable([{"rank": "A"}], [{"rank": "E"}])
    print("HELPERS_OK")

    sent = await svc._run_card_trades_unlocked(offers=5)
    print(json.dumps({"sent": sent, "trades_total": svc.stats.trades_sent}, ensure_ascii=False))
    await svc.stop()


if __name__ == "__main__":
    asyncio.run(main())
