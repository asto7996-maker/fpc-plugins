"""Юнит-тесты AuctionTrader (парсинг денег, лотов, торгового чата, лимиты)."""

from __future__ import annotations

from bs4 import BeautifulSoup

from dwar_bot.modules.auction_trader import (
    AuctionItem,
    AuctionTrader,
    TradeOffer,
    copper_to_gold,
    copper_to_money_dict,
    format_money,
    gold_to_copper,
    money_dict_to_copper,
    parse_money_text,
)
from dwar_bot.modules.stats_parser import PlayerStats


def test_money_roundtrip() -> None:
    assert money_dict_to_copper({"gold": 1, "silver": 2, "copper": 3}) == 10203
    assert copper_to_money_dict(10203) == {"gold": 1, "silver": 2, "copper": 3}
    assert gold_to_copper(1.5) == 15_000
    assert abs(copper_to_gold(15_000) - 1.5) < 1e-9


def test_parse_money_text() -> None:
    assert parse_money_text("1з 50с 0м")["gold"] == 1
    assert parse_money_text("1з 50с 0м")["silver"] == 50
    dotted = parse_money_text("2.3.45")
    assert dotted == {"gold": 2, "silver": 3, "copper": 45}
    assert "з" in format_money(dotted)


def test_trade_offer_matches_and_unit_price() -> None:
    offer = TradeOffer(item_name="Малый эликсир", target_price=0.5, max_quantity=3)
    assert offer.matches("Малый эликсир жизни")
    assert not offer.matches("Свиток телепорта")

    lot = AuctionItem(
        item_id="42",
        name="Малый эликсир жизни",
        count=2,
        buyout_price={"gold": 0, "silver": 80, "copper": 0},
    )
    # 0.8з / 2 = 0.4з за шт. ≤ 0.5
    assert lot.unit_buyout_gold == 0.4
    assert lot.unit_buyout_gold <= offer.target_price


def test_can_afford_respects_reserve() -> None:
    trader = AuctionTrader(reserve_gold=1.0)
    stats = PlayerStats(gold=2, silver=0, copper=0)  # 2з
    cheap = AuctionItem(
        item_id="1",
        name="Трава",
        buyout_price={"gold": 0, "silver": 50, "copper": 0},  # 0.5з
    )
    expensive = AuctionItem(
        item_id="2",
        name="Руда",
        buyout_price={"gold": 1, "silver": 50, "copper": 0},  # 1.5з → остаток 0.5 < резерв 1
    )
    assert trader._can_afford(stats, cheap) is True
    assert trader._can_afford(stats, expensive) is False


def test_parse_lots_from_soup() -> None:
    html = """
    <table class="auction">
      <tr class="lot" data-lot-id="777">
        <td class="item-name">Аметист</td>
        <td class="count">x3</td>
        <td class="bid">0з 10с 0м</td>
        <td class="buyout">0з 25с 0м</td>
        <td class="time">2ч 15м</td>
        <td><a href="/auction.php?buy=777">Купить</a></td>
      </tr>
    </table>
    """
    soup = BeautifulSoup(html, "html.parser")
    trader = AuctionTrader()
    lots = trader._parse_lots_from_soup(soup, category_id="ore")
    assert len(lots) == 1
    lot = lots[0]
    assert lot.item_id == "777"
    assert "Аметист" in lot.name
    assert lot.count == 3
    assert lot.buyout_price["silver"] == 25
    assert "buy=777" in lot.buy_href


def test_parse_trade_chat_message() -> None:
    trader = AuctionTrader()
    sell = trader._parse_trade_message("Продам малый эликсир жизни x2 за 0з 40с 0м")
    assert sell is not None
    assert sell["side"] == "sell"
    assert sell["count"] == 2
    assert sell["unit_gold"] == 0.2
    assert sell["score"] > 0

    buy = trader._parse_trade_message("Куплю руда железо дорого 5з 0с 0м")
    assert buy is not None
    assert buy["side"] == "buy"
    assert buy["hint"] in {"руда", "железо"}
    assert buy["price"]["gold"] == 5

    junk = trader._parse_trade_message("привет всем")
    assert junk is None


def test_default_bid_is_below_buyout() -> None:
    buyout = {"gold": 1, "silver": 0, "copper": 0}
    bid = AuctionTrader._default_bid(buyout)
    assert money_dict_to_copper(bid) < money_dict_to_copper(buyout)
    assert money_dict_to_copper(bid) == 7000
