from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repo" / "src"))

from shop.quote import build_quote


def test_discount_can_remove_free_shipping_eligibility():
    result = build_quote([{"sku": "A", "price": "55.00", "quantity": 1}], discount=Decimal("10.00"))
    assert result == {"subtotal": Decimal("55.00"), "shipping": Decimal("5.00"), "tax": Decimal("5.00"), "total": Decimal("55.00")}


def test_discount_at_threshold_still_gets_free_shipping():
    result = build_quote([{"sku": "A", "price": "60.00", "quantity": 1}], discount=Decimal("10.00"))
    assert result["shipping"] == Decimal("0.00")
    assert result["tax"] == Decimal("5.00")
    assert result["total"] == Decimal("55.00")


def test_zero_discount_keeps_existing_free_shipping():
    result = build_quote([{"sku": "A", "price": "50.00", "quantity": 1}], discount=Decimal("0"))
    assert result["shipping"] == Decimal("0.00")


def test_rejects_negative_discount():
    try:
        build_quote([{"price": "10", "quantity": 1}], discount=Decimal("-0.01"))
    except ValueError:
        return
    raise AssertionError("negative discount was accepted")


def test_rejects_discount_larger_than_subtotal():
    try:
        build_quote([{"price": "10", "quantity": 1}], discount=Decimal("10.01"))
    except ValueError:
        return
    raise AssertionError("discount larger than subtotal was accepted")


def test_inputs_are_unchanged():
    items = [{"sku": "A", "price": "55.00", "quantity": 1}]
    before = [dict(item) for item in items]
    build_quote(items, discount=Decimal("10"))
    assert items == before


