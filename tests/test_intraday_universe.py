"""The engine is focused on 0DTE SPX/NDX exposure via the SPY/QQQ proxies.

The single-stock intraday tier and the swing/weekly stock universe were
removed; these tests pin the universe so a stray symbol can't silently
reintroduce per-stock data pulls.
"""
import main


def test_intraday_universe_is_etfs_only():
    assert main.INTRADAY_SYMBOLS == main.ETF_PRODUCTS


def test_etf_products_unchanged():
    # 0DTE option selection / opening-drive gating keys off this set, so it
    # must stay exactly SPY/QQQ.
    assert main.ETF_PRODUCTS == ["SPY", "QQQ"]


def test_symbols_alias_matches_products():
    # Aux sweeps (IV/OI, dashboards) iterate SYMBOLS; it must not regrow a
    # stock universe.
    assert main.SYMBOLS == main.ETF_PRODUCTS


def test_index_products_context_only():
    assert main.INDEX_PRODUCTS == ["SPX", "NDX"]


def test_product_class_derivation():
    # Mirrors the per-symbol tagging in scan_all_symbols.
    pc = lambda s: "ETF" if s in main.ETF_PRODUCTS else "STOCK"
    assert pc("SPY") == "ETF"
    assert pc("QQQ") == "ETF"
