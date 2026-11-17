from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")


def shipping_for(subtotal: Decimal, method: str) -> Decimal:
    if method == "standard":
        fee = Decimal("0.00") if subtotal >= Decimal("50.00") else Decimal("5.00")
    elif method == "express":
        fee = Decimal("12.00")
    else:
        raise ValueError(f"unknown shipping method: {method}")
    return fee.quantize(CENT, rounding=ROUND_HALF_UP)
