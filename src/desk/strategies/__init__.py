"""Portfolio and trend research books E1–E6.

Public Binance USDT-M data only. These modules size a simulated book.
They do not place orders, sign requests, or read trading keys.
`mode: live` is refused by the runner, the same way the scanner refuses it.
"""

__all__ = ["require_research_mode"]


def require_research_mode(settings: dict) -> None:
    """Research books run only in paper mode. Live mode has no order path."""
    from desk.config import assert_no_live_orders

    assert_no_live_orders(settings)
