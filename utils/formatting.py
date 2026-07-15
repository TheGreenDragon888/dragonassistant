"""
utils/formatting.py

Shared helpers so every command displays currency the same way.
"""
import math

DEFAULT_CURRENCY_EMOJI = "💰"


def format_currency(amount: float, emoji: str | None = None) -> str:
    """The server's currency emoji preceding the value, rounded DOWN to the
    nearest cent (2 decimals) for display. This only affects what's shown -
    the underlying balance keeps its full floating-point precision in the
    database; use format_market_currency instead for market prices, which
    are often worth fractions of a cent and need finer display precision."""
    floored_cents = math.floor(amount * 100 + 1e-9) / 100
    return f"{emoji or DEFAULT_CURRENCY_EMOJI} {floored_cents:,.2f}"


def format_market_currency(amount: float, emoji: str | None = None) -> str:
    """Same as format_currency but shows 4 decimal places without flooring -
    scoped to the market (docs/market.md), where raw materials frequently
    trade for a fraction of a cent and 2 decimals would round many prices
    straight to 0.00."""
    return f"{emoji or DEFAULT_CURRENCY_EMOJI} {amount:,.4f}"


def format_compact_number(value: float) -> str:
    """A bare number (no currency symbol) sized for tight table columns:
    plain 4-decimal form if that fits in 6 characters, otherwise abbreviated
    with a K/M/B suffix so large market prices don't blow out the column
    width."""
    plain = f"{value:.4f}"
    if len(plain) <= 6:
        return plain
    abs_value = abs(value)
    if abs_value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if abs_value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs_value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.1f}"
