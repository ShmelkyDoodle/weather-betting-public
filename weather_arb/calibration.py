"""Empirical probability recalibration for Strategy 2 (model edge).

Diagnosis (2026-08-14, first honest 21-day backtest on real lead-time data,
see :mod:`backtest`): the forecast models are well-calibrated *in aggregate*
(Brier 0.14-0.19, ~80% directional accuracy against Kalshi-sourced settlements),
but the edge-trade selection rule -- "take the biggest apparent mispricing" --
systematically walks into the models' worst blind spot. Cheap (<=20c) YES
entries, where the model claims a real chance the market prices near-zero, hit
only ~2% of the time. Those buckets sit under 1 standard deviation from the
model's own point forecast with sigma averaging ~4.5F: a sigma that wide smears
meaningful probability across many adjacent 2F buckets near the forecast, none
individually looking implausible to the model, when in reality only one bucket
ever happens and the market prices the neighbors correctly near zero. That is a
**sigma-model miscalibration**, not a genuine market inefficiency to exploit.

Rather than patch the sigma formula by guesswork, this module lets three weeks
of real settlements answer directly: "when the model says X%, how often does
that side actually win?" -- via isotonic regression (Pool Adjacent Violators),
binned by raw probability. The result is a monotonic recalibration curve applied
to a probability *before* it is used to compute a trade's edge. Backtest-validated
with a chronological train/test split (never fit and evaluate on the same trades)
before ever touching the live path; live use is opt-in (see :mod:`arb_engine`).

No new dependency: isotonic regression is ~20 lines of pure Python (a textbook
algorithm), consistent with this project's dependency-free ethos.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


def _pava(values: list[float], weights: list[float]) -> list[float]:
    """Pool-Adjacent-Violators: weighted values (x-sorted) -> the non-decreasing
    step sequence minimizing weighted squared error. Returns exactly ``len(values)``
    outputs -- one per input element, constant within each pooled block.

    Expansion is by *original element count*, not by (rounded) pooled weight --
    weights need not be integers (e.g. bin counts averaged elsewhere), and using
    weight to size the expansion would silently return the wrong number of
    outputs whenever a block's pooled weight differs from how many input elements
    it absorbed.
    """
    blocks: list[list[float]] = []  # [mean, weight, element_count] per block, in x-order
    for v, w in zip(values, weights):
        blocks.append([v, w, 1])
        while len(blocks) >= 2 and blocks[-2][0] > blocks[-1][0]:
            v2, w2, c2 = blocks.pop()
            v1, w1, c1 = blocks.pop()
            nw = w1 + w2
            blocks.append([(v1 * w1 + v2 * w2) / nw, nw, c1 + c2])
    out: list[float] = []
    for mean, _w, count in blocks:
        out.extend([mean] * count)
    return out


def bin_stats(
    pairs: list[tuple[float, float]], n_bins: int = 20, min_bin_samples: int = 15,
) -> list[tuple[float, float, int]]:
    """Group ``(raw_prob, outcome)`` pairs into equal-width ``[0,1]`` bins.

    Returns ``(mean_prob, mean_outcome, count)`` for bins with at least
    ``min_bin_samples`` points, sorted by probability. Binning first (rather than
    fitting PAVA on thousands of raw points) smooths sampling noise and keeps the
    fitted curve's breakpoint table small enough to persist.
    """
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(n_bins)]
    for x, y in pairs:
        idx = min(n_bins - 1, max(0, int(x * n_bins)))
        buckets[idx].append((x, y))
    out: list[tuple[float, float, int]] = []
    for b in buckets:
        if len(b) < min_bin_samples:
            continue
        n = len(b)
        out.append((sum(x for x, _ in b) / n, sum(y for _, y in b) / n, n))
    return out


@dataclass(frozen=True, slots=True)
class CalibrationMap:
    """A fitted, monotonic probability recalibration curve.

    ``breakpoints`` are ``(raw_prob, calibrated_prob)`` pairs, x-sorted,
    non-decreasing in y (the isotonic fit). :meth:`apply` linearly interpolates
    between breakpoints and clamps outside their range.
    """

    breakpoints: tuple[tuple[float, float], ...]
    n_samples: int

    def apply(self, raw_prob: float) -> float:
        """Recalibrated probability for ``raw_prob`` in ``[0, 1]``.

        With no fitted breakpoints (insufficient data), returns ``raw_prob``
        unchanged -- calibration degrades to a no-op, never to a crash or a
        fabricated correction.
        """
        bps = self.breakpoints
        if not bps:
            return raw_prob
        if raw_prob <= bps[0][0]:
            return bps[0][1]
        if raw_prob >= bps[-1][0]:
            return bps[-1][1]
        for (x0, y0), (x1, y1) in zip(bps, bps[1:]):
            if x0 <= raw_prob <= x1:
                if x1 == x0:
                    return y0
                t = (raw_prob - x0) / (x1 - x0)
                return y0 + t * (y1 - y0)
        return bps[-1][1]  # unreachable given the bounds checks above


def fit_calibration(
    pairs: list[tuple[float, float]], n_bins: int = 20, min_bin_samples: int = 15,
) -> Optional[CalibrationMap]:
    """Fit an isotonic :class:`CalibrationMap` from ``(raw_prob, outcome)`` pairs.

    ``outcome`` is 1.0/0.0 (did the side this probability was for actually win).
    Returns ``None`` if too few bins qualify (``min_bin_samples`` each) to fit a
    meaningful curve -- callers should fall back to uncalibrated probabilities.
    """
    stats = bin_stats(pairs, n_bins=n_bins, min_bin_samples=min_bin_samples)
    if len(stats) < 2:
        return None
    xs = [x for x, _, _ in stats]
    ys = [y for _, y, _ in stats]
    ws = [float(n) for _, _, n in stats]
    fitted = _pava(ys, ws)
    breakpoints = tuple(zip(xs, fitted))
    return CalibrationMap(breakpoints=breakpoints, n_samples=sum(int(w) for w in ws))


def serialize(cm: CalibrationMap) -> str:
    """Compact ``"x0,y0;x1,y1;..."`` encoding for DB storage."""
    return ";".join(f"{x:.6f},{y:.6f}" for x, y in cm.breakpoints)


def deserialize(text: str, n_samples: int) -> Optional[CalibrationMap]:
    """Inverse of :func:`serialize`; ``None`` for empty/unparseable input."""
    if not text:
        return None
    try:
        bps = tuple(
            (float(a), float(b))
            for a, b in (pair.split(",") for pair in text.split(";") if pair)
        )
    except ValueError:
        return None
    return CalibrationMap(breakpoints=bps, n_samples=n_samples) if bps else None
