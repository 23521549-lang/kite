"""Error bars for the bench: how sure a pass rate is, and whether a change made a real difference.

With 34 polyglot exercises a pass rate moves by several points from noise alone (about ±17 points at
95% around 50%), so a single run, or two runs compared by their totals, says little. This module gives:

* ``wilson`` — the 95% interval of one pass rate (Wilson's score interval: sane at 0/n and n/n, where
  the plain normal interval collapses to a width of zero).
* ``mean_se`` — mean and standard error of scores (an axis, a per-task pass rate).
* ``mcnemar_exact`` — the exact two-sided McNemar test for two configurations run on the same tasks:
  only the tasks where they disagree count.
* ``paired`` — two configurations task by task. Each side may be several runs of the same
  configuration (run `kite bench run` again with another label): a task's rate is then its mean over
  those runs, and the error of the difference is taken over tasks, not over runs (tasks are the
  clusters, Miller 2024, "Adding Error Bars to Evals", arXiv:2411.00640).

Standard library only.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

Z95 = 1.959963984540054


def wilson(passed: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """95% interval of a pass rate (0..1); None without tasks."""
    if n <= 0:
        return None
    p = passed / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def mean_se(values: Iterable[float]) -> tuple[float | None, float | None]:
    """Mean and its standard error (sample standard deviation / sqrt(n)); no error below two values."""
    values = list(values)
    if not values:
        return None, None
    mean = sum(values) / len(values)
    if len(values) < 2:
        return mean, None
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return mean, math.sqrt(variance / len(values))


def mcnemar_exact(only_a: int, only_b: int) -> float:
    """Two-sided p of the exact McNemar test: under "no difference" each disagreeing task is a coin flip."""
    n = only_a + only_b
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(only_a, only_b) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def task_rates(runs: Iterable[Mapping[str, Any]], *, metric: str = "passed") -> dict[str, float]:
    """Per-task pass rate over one or more runs of one configuration: ``runs`` are model entries of
    results.json (their ``lead``, ``code`` and ``v1`` outcomes). A task missing from a run is not
    counted for that run; ``pass_1`` only exists for polyglot exercises."""
    seen: dict[str, list[float]] = {}
    for entry in runs:
        for suite in ("lead", "code", "v1"):
            for item in entry.get(suite) or []:
                if "id" in item and metric in item:
                    seen.setdefault(str(item["id"]), []).append(1.0 if item[metric] else 0.0)
    return {task: sum(v) / len(v) for task, v in seen.items()}


def paired(a: Mapping[str, float], b: Mapping[str, float], *, alpha: float = 0.05) -> dict[str, Any]:
    """Configuration A against B on the tasks both ran. With one run a side every rate is 0 or 1 and
    the verdict is McNemar's exact test; with repeated runs, whether the 95% interval of the mean
    per-task difference (B - A) leaves out zero."""
    common = sorted(set(a) & set(b))
    diffs = [b[task] - a[task] for task in common]
    mean, se = mean_se(diffs)
    ci = (mean - Z95 * se, mean + Z95 * se) if mean is not None and se is not None else None
    binary = all(a[t] in (0.0, 1.0) and b[t] in (0.0, 1.0) for t in common)
    only_a = [t for t in common if a[t] > b[t]]
    only_b = [t for t in common if b[t] > a[t]]
    p_value = mcnemar_exact(len(only_a), len(only_b)) if binary else None
    if not common:
        significant = False
    elif binary:
        significant = p_value < alpha
    else:
        significant = ci is not None and (ci[0] > 0 or ci[1] < 0)
    return {"tasks": len(common), "only_in_a": sorted(set(a) - set(b)), "only_in_b": sorted(set(b) - set(a)),
            "rate_a": sum(a[t] for t in common) / len(common) if common else None,
            "rate_b": sum(b[t] for t in common) / len(common) if common else None,
            "diff": mean, "diff_se": se, "diff_ci95": ci,
            "a_better": only_a, "b_better": only_b, "mcnemar_p": p_value, "significant": significant}
