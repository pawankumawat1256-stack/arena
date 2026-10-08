"""Deterministic fee and royalty math. Models never calculate money; this module does.

Rules encoded here (checked against the KDP, Gumroad and Payhip pages during research):
- KDP royalty: 70% x (list price - VAT - delivery cost) in the 70% band; 35% x (list price - VAT) otherwise.
- KDP 70% band: US$2.99 to US$12.99 on Amazon.com; INR 99 to 599 including GST on Amazon.in.
- KDP delivery cost: US$0.15 per MB on Amazon.com; INR 7 per MB on Amazon.in.
- India: 18% GST is included in the list price and removed before royalties are calculated.
- Gumroad direct fee: 10% + US$0.50 per sale. Gumroad Discover: 30%.
- Payhip: Free 5%, Plus 2% for US$29 per month, Pro 0% for US$99 per month. Processor fees are extra.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

CENT = Decimal("0.01")
ZERO = Decimal("0")
ONE = Decimal("1")

APPROVED_KDP_PRICE = Decimal("9.99")
KDP_US_70_MIN = Decimal("2.99")
KDP_US_70_MAX = Decimal("12.99")
KDP_US_DELIVERY_PER_MB = Decimal("0.15")
KDP_IN_70_MIN_INR = Decimal("99")
KDP_IN_70_MAX_INR = Decimal("599")
KDP_IN_DELIVERY_PER_MB_INR = Decimal("7")
INDIA_GST_RATE = Decimal("0.18")
ROYALTY_70 = Decimal("0.70")
ROYALTY_35 = Decimal("0.35")

GUMROAD_RATE = Decimal("0.10")
GUMROAD_FLAT_USD = Decimal("0.50")
GUMROAD_DISCOVER_RATE = Decimal("0.30")

PAYHIP_PLANS: dict[str, dict[str, Decimal]] = {
    "free": {"rate": Decimal("0.05"), "monthly": Decimal("0")},
    "plus": {"rate": Decimal("0.02"), "monthly": Decimal("29")},
    "pro": {"rate": Decimal("0"), "monthly": Decimal("99")},
}


def q(value: Decimal | float | int | str) -> Decimal:
    """Round to cents, half up."""
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Royalty:
    marketplace: str
    option: str  # "70%" or "35%"
    price_before_tax: Decimal
    delivery_cost: Decimal
    royalty: Decimal


def kdp_us_royalty(list_price_usd: Decimal | float | str, file_mb: Decimal | float | str) -> Royalty:
    price = Decimal(str(list_price_usd))
    mb = Decimal(str(file_mb))
    if KDP_US_70_MIN <= price <= KDP_US_70_MAX:
        delivery = mb * KDP_US_DELIVERY_PER_MB
        return Royalty("Amazon.com", "70%", price, q(delivery), q(ROYALTY_70 * (price - delivery)))
    return Royalty("Amazon.com", "35%", price, q(ZERO), q(ROYALTY_35 * price))


def kdp_break_even_mb(list_price_usd: Decimal | float | str) -> Decimal:
    """File size (MB) at which the 70% royalty equals the 35% royalty. Derivation: 0.35P = 0.105M, so M = 10P/3."""
    price = Decimal(str(list_price_usd))
    return q(price * Decimal(10) / Decimal(3))


def kdp_in_royalty(list_price_inr: Decimal | float | str, file_mb: Decimal | float | str, *, kdp_select: bool) -> Royalty:
    price = Decimal(str(list_price_inr))
    mb = Decimal(str(file_mb))
    before_tax = q(price / (ONE + INDIA_GST_RATE))
    if kdp_select and KDP_IN_70_MIN_INR <= price <= KDP_IN_70_MAX_INR:
        delivery = mb * KDP_IN_DELIVERY_PER_MB_INR
        return Royalty("Amazon.in", "70%", before_tax, q(delivery), q(ROYALTY_70 * (before_tax - delivery)))
    return Royalty("Amazon.in", "35%", before_tax, q(ZERO), q(ROYALTY_35 * before_tax))


def after_withholding(amount: Decimal, rate: Decimal) -> Decimal:
    return q(amount * (ONE - rate))


def gumroad_fee(amount_usd: Decimal | float | str, *, discover: bool = False) -> Decimal:
    amount = Decimal(str(amount_usd))
    if discover:
        return q(amount * GUMROAD_DISCOVER_RATE)
    return q(amount * GUMROAD_RATE + GUMROAD_FLAT_USD)


def payhip_fee(amount_usd: Decimal | float | str, plan: str = "free") -> Decimal:
    return q(Decimal(str(amount_usd)) * PAYHIP_PLANS[plan]["rate"])


def payhip_break_even_monthly_sales(from_plan: str, to_plan: str) -> Decimal | None:
    """Monthly sales (USD) above which to_plan costs less than from_plan. None if to_plan never wins."""
    rate_saving = PAYHIP_PLANS[from_plan]["rate"] - PAYHIP_PLANS[to_plan]["rate"]
    extra_monthly = PAYHIP_PLANS[to_plan]["monthly"] - PAYHIP_PLANS[from_plan]["monthly"]
    if rate_saving <= ZERO:
        return None
    return q(extra_monthly / rate_saving)


def validate_tiers(basic: Decimal, toolkit: Decimal, bundle: Decimal) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors stop the price stage. Warnings are shown to the owner for a decision."""
    errors: list[str] = []
    warnings: list[str] = []
    if toolkit <= ZERO or bundle <= ZERO or basic <= ZERO:
        errors.append("Prices must be greater than zero.")
    if not KDP_US_70_MIN <= basic <= KDP_US_70_MAX:
        errors.append("Basic price is outside the KDP 70% band ($2.99 to $12.99); the royalty would drop to 35%.")
    if basic != APPROVED_KDP_PRICE:
        warnings.append(f"Basic price is ${basic}; the approved KDP price is ${APPROVED_KDP_PRICE}.")
    if bundle >= basic + toolkit:
        warnings.append(
            f"Bundle at ${bundle} does not save money against Basic plus Pro (${q(basic + toolkit)}). "
            "A bundle is a discount only when it costs less than the two parts."
        )
    return errors, warnings


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def build_pricing_plan(
    *,
    basic_usd: float | str | Decimal,
    file_mb: float | str | Decimal,
    toolkit_usd: float | str | Decimal,
    bundle_usd: float | str | Decimal,
    withholding_rate: Decimal | float | str = Decimal("0.10"),
) -> dict[str, Any]:
    """Every number the PricingAgent may quote. Returned as strings so it serialises exactly."""
    basic = Decimal(str(basic_usd))
    toolkit = Decimal(str(toolkit_usd))
    bundle = Decimal(str(bundle_usd))
    mb = Decimal(str(file_mb))
    rate = Decimal(str(withholding_rate))

    us = kdp_us_royalty(basic, mb)
    india_select = kdp_in_royalty(Decimal("399"), mb, kdp_select=True)
    india_wide = kdp_in_royalty(Decimal("399"), mb, kdp_select=False)
    separate = basic + toolkit
    errors, warnings = validate_tiers(basic, toolkit, bundle)

    def gumroad_block(price: Decimal) -> dict[str, str | None]:
        direct = gumroad_fee(price)
        discover = gumroad_fee(price, discover=True)
        return {
            "direct_fee_usd": _s(direct),
            "direct_net_usd": _s(q(price - direct)),
            "discover_fee_usd": _s(discover),
            "discover_net_usd": _s(q(price - discover)),
        }

    def payhip_block(price: Decimal) -> dict[str, str | None]:
        fee = payhip_fee(price, "free")
        return {"payhip_free_fee_usd": _s(fee), "payhip_free_net_before_processor_usd": _s(q(price - fee))}

    return {
        "basic_kdp_us": {
            "option": us.option,
            "delivery_usd": _s(us.delivery_cost),
            "royalty_usd": _s(us.royalty),
            "royalty_if_35_percent_usd": _s(q(ROYALTY_35 * basic)),
            "break_even_file_size_mb": _s(kdp_break_even_mb(basic)),
        },
        "basic_kdp_india_example_399_inr": {
            "price_before_gst_inr": _s(india_select.price_before_tax),
            "with_kdp_select": {"option": india_select.option, "royalty_inr": _s(india_select.royalty)},
            "without_kdp_select": {"option": india_wide.option, "royalty_inr": _s(india_wide.royalty)},
            "withholding_rate": _s(rate),
            "after_withholding_with_kdp_select_inr": _s(after_withholding(india_select.royalty, rate)),
        },
        "pro_toolkit": {"price_usd": _s(q(toolkit)), **gumroad_block(toolkit), **payhip_block(toolkit)},
        "bundle": {"price_usd": _s(q(bundle)), **gumroad_block(bundle), **payhip_block(bundle)},
        "bundle_discount": {
            "basic_plus_pro_usd": _s(q(separate)),
            "bundle_usd": _s(q(bundle)),
            "saving_usd": _s(q(separate - bundle)),
            "saving_percent": _s(q((separate - bundle) / separate * 100)) if separate else None,
        },
        "payhip_break_even_monthly_sales_usd": {
            "free_to_plus": _s(payhip_break_even_monthly_sales("free", "plus")),
            "plus_to_pro": _s(payhip_break_even_monthly_sales("plus", "pro")),
        },
        "errors": errors,
        "warnings": warnings,
    }
