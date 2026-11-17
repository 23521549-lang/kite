from decimal import Decimal
from shop.quote import build_quote


def test_standard_shipping_below_threshold():
    result = build_quote([{"sku": "A", "price": "20.00", "quantity": 1}])
    assert result == {"subtotal": Decimal("20.00"), "shipping": Decimal("5.00"), "tax": Decimal("2.50"), "total": Decimal("27.50")}


def test_standard_shipping_at_threshold_is_free():
    result = build_quote([{"sku": "A", "price": "50.00", "quantity": 1}])
    assert result["shipping"] == Decimal("0.00")


def test_express_shipping_flat_rate():
    result = build_quote([{"sku": "A", "price": "50.00", "quantity": 1}], shipping_method="express")
    assert result["shipping"] == Decimal("12.00")
