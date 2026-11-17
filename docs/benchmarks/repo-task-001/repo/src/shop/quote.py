from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence

from shop.shipping import CENT, shipping_for

ZERO = Decimal("0.00")
DEFAULT_TAX_RATE = Decimal("0.10")


def build_quote(
    items: Sequence[Mapping[str, object]],
    shipping_method: str = "standard",
    discount: Decimal = ZERO,
) -> dict[str, Decimal]:
    """Build an order quote. Prices and discounts are decimal currency amounts."""
    subtotal = sum(
        (Decimal(str(item["price"])) * int(item["quantity"]) for item in items),
        start=ZERO,
    ).quantize(CENT, rounding=ROUND_HALF_UP)
    discount = Decimal(str(discount)).quantize(CENT, rounding=ROUND_HALF_UP)
    # BUG: the shipping threshold must use the post-discount merchandise amount.
    shipping = shipping_for(subtotal, shipping_method)
    taxable = subtotal - discount + shipping
    tax = (taxable * DEFAULT_TAX_RATE).quantize(CENT, rounding=ROUND_HALF_UP)
    total = (taxable + tax).quantize(CENT, rounding=ROUND_HALF_UP)
    return {"subtotal": subtotal, "shipping": shipping, "tax": tax, "total": total}
