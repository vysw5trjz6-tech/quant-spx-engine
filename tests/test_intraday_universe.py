"""The intraday tier now scans liquid single stocks, not just SPY/QQQ.

Trend days used to surface nothing tradeable because the intraday universe was
ETFs only and the weekly/stock tier was a separate, slower pass. These tests
pin the universe wiring: ETFs still carry 0DTE, the added names are STOCKs that
resolve to weekly contracts, and the two sets stay disjoint/consistent.
"""
import main


def test_intraday_universe_includes_stocks():
    assert "SPY" in main.INTRADAY_SYMBOLS and "QQQ" in main.INTRADAY_SYMBOLS
    assert "NVDA" in main.INTRADAY_SYMBOLS
    # Strictly larger than the old ETF-only universe.
    assert len(main.INTRADAY_SYMBOLS) > len(main.ETF_PRODUCTS)


def test_intraday_symbols_is_etfs_plus_stocks():
    assert main.INTRADAY_SYMBOLS == main.ETF_PRODUCTS + main.INTRADAY_STOCKS


def test_etf_products_unchanged():
    # 0DTE option selection / opening-drive gating keys off this set, so it
    # must stay exactly SPY/QQQ even as the intraday universe grows.
    assert main.ETF_PRODUCTS == ["SPY", "QQQ"]


def test_intraday_stocks_are_not_etfs():
    for s in main.INTRADAY_STOCKS:
        assert s not in main.ETF_PRODUCTS


def test_product_class_derivation():
    # Mirrors the per-symbol tagging in scan_all_symbols.
    pc = lambda s: "ETF" if s in main.ETF_PRODUCTS else "STOCK"
    assert pc("SPY") == "ETF"
    assert pc("QQQ") == "ETF"
    assert pc("NVDA") == "STOCK"
    assert pc("TSLA") == "STOCK"
