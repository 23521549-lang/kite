# Repo task 001: discount-aware free shipping

This synthetic, disposable Python repository is an evaluation fixture, not Kite product code.

## Candidate task

Fix `shop.quote.build_quote` so a fixed discount reduces the merchandise subtotal before checking the free-shipping threshold. Tax must be calculated on discounted merchandise plus shipping. Preserve the public function signature and `Decimal` money handling. Reject a negative discount or one larger than the merchandise subtotal with `ValueError`. Do not mutate the input item mappings or sequence. Keep changes focused; return a unified diff and a brief explanation.

Repository instructions: run `python -m pytest -q` from this directory. Do not add dependencies. Shipping rules are documented in `README.md` and implemented in `src/shop/shipping.py`.

## Evaluation

The candidate sees `README.md`, `src/shop/quote.py`, `src/shop/shipping.py`, and `tests/test_quote.py`. Hidden checks are in `grading/test_hidden.py` and must not be supplied in the model prompt. Apply the model's patch to a clean copy and run both visible and hidden checks. Record exact model ID, prompt/template settings, generation time, patch validity, files changed, visible/hidden result, and any manual correction. One task is a qualitative repo-context probe, not evidence of production coding quality.
