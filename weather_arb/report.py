"""Performance report over the trade ledger.

Run any time (the process can keep running -- WAL allows concurrent reads)::

    python -m weather_arb.report            # human-readable summary
    python -m weather_arb.report --json     # machine-readable
    python -m weather_arb.report --db path/to/weather_arb.sqlite3

It joins ``trades`` with ``market_results`` to compute realized P&L, hit rate and
per-strategy / per-day breakdowns.  Unsettled fills are reported separately so
you can see how much of the book is still open.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from typing import Any

from .config import Settings
from .fees import fee_cents
from .pnl import trade_pnl_cents, trade_won


def build_summary(db_path: str, *, fee_coefficient: float = 0.07) -> dict[str, Any]:
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"ledger not found: {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        trades = conn.execute("SELECT * FROM trades").fetchall()
        results = {
            row["ticker"]: row["result"]
            for row in conn.execute("SELECT ticker, result FROM market_results")
        }
        open_orders = conn.execute(
            "SELECT COUNT(*) AS n FROM open_limit_orders "
            "WHERE status IN ('pending','resting','partially_filled')"
        ).fetchone()["n"]
        try:
            model_rows = conn.execute(
                "SELECT ticker, model, prob FROM model_predictions"
            ).fetchall()
        except sqlite3.OperationalError:
            model_rows = []
    finally:
        conn.close()

    def _strat_row() -> dict[str, float]:
        return {"fills": 0, "contracts": 0, "settled": 0, "wins": 0,
                "gross_usd": 0.0, "fees_usd": 0.0, "net_usd": 0.0}

    by_strategy: dict[str, dict[str, float]] = defaultdict(_strat_row)
    by_day: dict[str, float] = defaultdict(float)
    total = {
        "fills": 0, "contracts": 0, "notional_usd": 0.0,
        "settled_fills": 0, "unsettled_fills": 0, "wins": 0,
        "gross_usd": 0.0, "fees_usd": 0.0, "net_usd": 0.0,
    }
    # Calibration samples for the model-edge strategy: (predicted_prob, outcome).
    calib: list[tuple[float, int]] = []

    for t in trades:
        strat = t["strategy"]
        agg = by_strategy[strat]
        agg["fills"] += 1
        agg["contracts"] += t["count"]
        total["fills"] += 1
        total["contracts"] += t["count"]
        total["notional_usd"] += (t["price_cents"] / 100.0) * t["count"]

        result = results.get(t["ticker"])
        if result is None:
            total["unsettled_fills"] += 1
            continue

        gross = trade_pnl_cents(
            side=t["side"], action=t["action"],
            price_cents=t["price_cents"], count=t["count"], result=result,
        ) / 100.0
        fee = fee_cents(t["price_cents"], t["count"], fee_coefficient) / 100.0
        net = gross - fee
        won = trade_won(side=t["side"], action=t["action"], result=result)
        day = str(t["executed_at_utc"])[:10]

        agg["settled"] += 1
        agg["wins"] += 1 if won else 0
        agg["gross_usd"] += gross
        agg["fees_usd"] += fee
        agg["net_usd"] += net
        by_day[day] += net
        total["settled_fills"] += 1
        total["wins"] += 1 if won else 0
        total["gross_usd"] += gross
        total["fees_usd"] += fee
        total["net_usd"] += net

        mp = t["model_prob"] if "model_prob" in t.keys() else None
        if strat == "model_edge" and mp is not None:
            calib.append((float(mp), 1 if won else 0))

    settled = total["settled_fills"]
    return {
        "db_path": db_path,
        "fee_coefficient": fee_coefficient,
        "totals": {
            **total,
            "hit_rate": (total["wins"] / settled) if settled else None,
            "open_orders": open_orders,
            "settled_markets": len(results),
        },
        "by_strategy": {
            k: {**v, "hit_rate": (v["wins"] / v["settled"]) if v["settled"] else None}
            for k, v in by_strategy.items()
        },
        "pnl_by_day": dict(sorted(by_day.items())),
        "calibration": _calibration(calib),
        "model_comparison": _model_comparison(model_rows, results),
    }


def _model_comparison(model_rows: list[Any], results: dict[str, str]) -> dict[str, Any]:
    """Per-model Brier score + directional accuracy over settled markets.

    ``model_rows`` are (ticker, model, prob) shadow predictions; a market counts
    once it has a settled result. Lower Brier is better; accuracy is how often
    ``prob >= 0.5`` matched the YES/NO outcome.
    """
    agg: dict[str, dict[str, float]] = defaultdict(
        lambda: {"n": 0, "sq_err": 0.0, "correct": 0}
    )
    for row in model_rows:
        result = results.get(row["ticker"])
        if result is None:
            continue
        outcome = 1 if result == "yes" else 0
        p = float(row["prob"])
        a = agg[row["model"]]
        a["n"] += 1
        a["sq_err"] += (p - outcome) ** 2
        if (p >= 0.5) == (outcome == 1):
            a["correct"] += 1
    out: dict[str, Any] = {}
    for model, a in agg.items():
        n = int(a["n"])
        out[model] = {
            "n": n,
            "brier": (a["sq_err"] / n) if n else None,
            "accuracy": (a["correct"] / n) if n else None,
        }
    return out


def _calibration(samples: list[tuple[float, int]]) -> dict[str, Any]:
    """Reliability bins + Brier score for model-edge predictions.

    Each sample is (predicted P[our side wins], outcome in {0,1}).  A well-
    calibrated model has realized win rate ≈ mean predicted in each bin, and a
    low Brier score (mean squared error between prediction and outcome).
    """
    if not samples:
        return {"n": 0, "brier": None, "bins": []}
    brier = sum((p - o) ** 2 for p, o in samples) / len(samples)
    bins: list[dict[str, Any]] = []
    for lo in range(0, 100, 10):
        hi = lo + 10
        lo_f, hi_f = lo / 100.0, hi / 100.0
        in_bin = [
            (p, o) for p, o in samples
            if (lo_f <= p < hi_f) or (hi == 100 and p == 1.0)
        ]
        if not in_bin:
            continue
        n = len(in_bin)
        bins.append({
            "range": f"{lo:02d}-{hi:02d}%",
            "n": n,
            "mean_predicted": sum(p for p, _ in in_bin) / n,
            "realized": sum(o for _, o in in_bin) / n,
        })
    return {"n": len(samples), "brier": brier, "bins": bins}


def render_text(summary: dict[str, Any]) -> str:
    t = summary["totals"]
    lines: list[str] = []
    lines.append("=" * 64)
    lines.append("  weather-arb performance report")
    lines.append(f"  ledger: {summary['db_path']}  (fee coeff {summary['fee_coefficient']})")
    lines.append("=" * 64)
    lines.append(f"  fills (total)      : {t['fills']}  ({t['contracts']} contracts)")
    lines.append(f"  notional traded    : ${t['notional_usd']:,.2f}")
    lines.append(f"  settled / open     : {t['settled_fills']} settled, "
                 f"{t['unsettled_fills']} unsettled, {t['open_orders']} working orders")
    lines.append(f"  settled markets    : {t['settled_markets']}")
    hr = t["hit_rate"]
    lines.append(f"  hit rate (settled) : {hr:.1%}" if hr is not None else
                 "  hit rate (settled) : n/a (nothing settled yet)")
    lines.append(f"  gross P&L          : ${t['gross_usd']:,.2f}")
    lines.append(f"  fees (est.)        : -${t['fees_usd']:,.2f}")
    lines.append(f"  NET P&L            : ${t['net_usd']:,.2f}")
    lines.append("-" * 64)
    lines.append("  by strategy (net):")
    if summary["by_strategy"]:
        for strat, v in summary["by_strategy"].items():
            hr_s = v["hit_rate"]
            hr_txt = f"{hr_s:.1%}" if hr_s is not None else "n/a"
            lines.append(
                f"    {strat:<12} fills={int(v['fills']):<4} "
                f"settled={int(v['settled']):<4} hit={hr_txt:<6} "
                f"net=${v['net_usd']:,.2f}  (gross ${v['gross_usd']:,.2f}, "
                f"fees ${v['fees_usd']:,.2f})"
            )
    else:
        lines.append("    (no fills yet)")
    lines.append("-" * 64)
    lines.append("  net P&L by day:")
    if summary["pnl_by_day"]:
        for day, pnl in summary["pnl_by_day"].items():
            lines.append(f"    {day}   ${pnl:,.2f}")
    else:
        lines.append("    (no settled days yet)")
    lines.append("-" * 64)
    cal = summary["calibration"]
    lines.append("  model-edge calibration (predicted P[win] vs realized):")
    if cal["n"]:
        lines.append(f"    Brier score: {cal['brier']:.4f}   (lower is better; "
                     f"0.25 = coin flip)   n={cal['n']}")
        lines.append(f"    {'bucket':<10}{'n':>5}{'predicted':>12}{'realized':>11}")
        for b in cal["bins"]:
            lines.append(
                f"    {b['range']:<10}{b['n']:>5}"
                f"{b['mean_predicted']:>11.1%}{b['realized']:>11.1%}"
            )
    else:
        lines.append("    (no settled model-edge trades yet)")
    lines.append("-" * 64)
    mc = summary.get("model_comparison", {})
    lines.append("  forecast model comparison (settled markets — lower Brier is better):")
    ranked = sorted((m for m in mc.items() if m[1]["brier"] is not None),
                    key=lambda kv: kv[1]["brier"])
    if ranked:
        lines.append(f"    {'model':<12}{'n':>5}{'Brier':>10}{'dir.acc':>10}")
        for model, v in ranked:
            lines.append(f"    {model:<12}{v['n']:>5}{v['brier']:>10.3f}{v['accuracy']:>9.0%}")
        lines.append(f"    -> best so far: {ranked[0][0]} (Brier {ranked[0][1]['brier']:.3f})")
    else:
        lines.append("    (accumulating — appears once shadow predictions settle)")
    lines.append("=" * 64)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="weather-arb performance report")
    parser.add_argument("--db", default=None, help="path to the SQLite ledger")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--html", nargs="?", const="report.html", default=None,
                        metavar="PATH",
                        help="write a visual HTML dashboard (default: report.html)")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    db_path = args.db or settings.db_path
    try:
        summary = build_summary(db_path, fee_coefficient=settings.kalshi_fee_coefficient)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.html is not None:
        from .report_html import render_html
        html = render_html(summary, source_label=db_path, standalone=True)
        with open(args.html, "w", encoding="utf-8") as handle:
            handle.write(html)
        print(f"wrote {args.html}  (open it in a browser)")
    elif args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(render_text(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
