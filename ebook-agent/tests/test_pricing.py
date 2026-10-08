"""Deterministic money math. Expected values were worked out by hand from the official formulas."""

from decimal import Decimal

from core import pricing


def test_kdp_us_70_percent_royalty_uses_official_formula():
    result = pricing.kdp_us_royalty("9.99", "3")
    assert result.option == "70%"
    assert result.delivery_cost == Decimal("0.45")
    assert result.royalty == Decimal("6.68")  # 0.70 x (9.99 - 0.45)


def test_kdp_us_royalty_falls_as_file_grows():
    assert pricing.kdp_us_royalty("9.99", "1").royalty == Decimal("6.89")
    assert pricing.kdp_us_royalty("9.99", "3").royalty == Decimal("6.68")
    assert pricing.kdp_us_royalty("9.99", "5").royalty == Decimal("6.47")


def test_kdp_break_even_file_size_is_about_33_mb_at_999():
    assert pricing.kdp_break_even_mb("9.99") == Decimal("33.30")


def test_kdp_us_price_outside_band_pays_35_percent():
    result = pricing.kdp_us_royalty("1.99", "3")
    assert result.option == "35%"
    assert result.royalty == Decimal("0.70")  # 0.35 x 1.99 = 0.6965


def test_kdp_band_edges_are_inclusive():
    assert pricing.kdp_us_royalty("2.99", "1").option == "70%"
    assert pricing.kdp_us_royalty("12.99", "1").option == "70%"
    assert pricing.kdp_us_royalty("13.00", "1").option == "35%"


def test_india_example_with_and_without_kdp_select():
    with_select = pricing.kdp_in_royalty("399", "3", kdp_select=True)
    without_select = pricing.kdp_in_royalty("399", "3", kdp_select=False)
    assert with_select.price_before_tax == Decimal("338.14")
    assert with_select.royalty == Decimal("222.00")  # 0.70 x (338.14 - 21)
    assert without_select.option == "35%"
    assert without_select.royalty == Decimal("118.35")


def test_gumroad_direct_fee_is_ten_percent_plus_fifty_cents():
    assert pricing.gumroad_fee("9.99") == Decimal("1.50")
    assert pricing.q(Decimal("9.99") - pricing.gumroad_fee("9.99")) == Decimal("8.49")
    assert pricing.gumroad_fee("24") == Decimal("2.90")
    assert pricing.gumroad_fee("29") == Decimal("3.40")


def test_gumroad_discover_fee_is_thirty_percent():
    assert pricing.gumroad_fee("19", discover=True) == Decimal("5.70")


def test_payhip_free_plan_fee():
    assert pricing.payhip_fee("19", "free") == Decimal("0.95")


def test_payhip_break_even_points():
    assert pricing.payhip_break_even_monthly_sales("free", "plus") == Decimal("966.67")
    assert pricing.payhip_break_even_monthly_sales("plus", "pro") == Decimal("3500.00")
    assert pricing.payhip_break_even_monthly_sales("pro", "plus") is None


def test_validate_tiers_is_clean_for_the_approved_ladder():
    assert pricing.validate_tiers(Decimal("9.99"), Decimal("19"), Decimal("24")) == ([], [])


def test_bundle_that_is_not_a_discount_is_a_warning_not_an_error():
    errors, warnings = pricing.validate_tiers(Decimal("9.99"), Decimal("19"), Decimal("29"))
    assert errors == []
    assert any("does not save money" in warning for warning in warnings)


def test_basic_price_outside_the_kdp_band_is_an_error():
    errors, _warnings = pricing.validate_tiers(Decimal("13.99"), Decimal("19"), Decimal("24"))
    assert any("70% band" in error for error in errors)


def test_basic_price_other_than_999_is_a_warning():
    errors, warnings = pricing.validate_tiers(Decimal("12.99"), Decimal("19"), Decimal("24"))
    assert errors == []
    assert any("approved KDP price" in warning for warning in warnings)


def test_build_pricing_plan_contains_exact_strings_and_discount():
    plan = pricing.build_pricing_plan(basic_usd=9.99, file_mb=3, toolkit_usd=19, bundle_usd=24)
    assert plan["basic_kdp_us"]["royalty_usd"] == "6.68"
    assert plan["bundle_discount"]["saving_usd"] == "4.99"
    assert plan["bundle"]["direct_fee_usd"] == "2.90"
    assert plan["errors"] == [] and plan["warnings"] == []
