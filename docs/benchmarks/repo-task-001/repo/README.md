# TinyShop quote fixture

Python 3.10+; no runtime dependencies. Run checks from this directory:

```console
python -m pytest -q
```

`build_quote` sums item prices times quantities. Standard shipping costs $5.00 when the **discounted merchandise subtotal** is below $50.00 and is free at or above $50.00. Express shipping costs $12.00 regardless of subtotal. A fixed discount reduces merchandise subtotal before the shipping threshold. Tax is 10% of discounted merchandise plus shipping, rounded to cents using half-up rounding. The returned `subtotal` field remains the pre-discount merchandise subtotal. Discounts must be between zero and the subtotal, inclusive. Inputs must not be changed.
