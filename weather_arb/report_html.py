"""Render the trade ledger as a self-contained, visual HTML dashboard.

``python -m weather_arb.report --html`` writes a single standalone HTML file
(no external assets) that turns the same numbers as the text report into a
plain-English, at-a-glance page: a traffic-light verdict, stat tiles, a
cumulative-P&L chart, a per-day bar chart, a Strategy-2 calibration diagram, an
auto-written interpretation of *your* numbers, and the 4-week operating plan.

Design: utilitarian data dashboard — summary first, semantic good/bad color
separate from the blue accent, system-ui type with tabular figures, light/dark
aware. Charts are static inline SVG (print- and screenshot-clean, zero JS deps).
"""

from __future__ import annotations

from typing import Any, Optional

# --- validated palette slots (see dataviz skill) ---------------------------
_GOOD = "#0ca30c"
_CRIT = "#d03b3b"
_WARN = "#fab219"
_ACCENT_LIGHT = "#2a78d6"
_ACCENT_DARK = "#3987e5"


def _fmt_money(x: float) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.2f}"


def _pct(x: Optional[float], digits: int = 0) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:.{digits}f}%"


# --------------------------------------------------------------------------- #
# Verdict + interpretation (plain English from the numbers)
# --------------------------------------------------------------------------- #
def _verdict(summary: dict[str, Any]) -> dict[str, str]:
    t = summary["totals"]
    strat = summary["by_strategy"]
    cal = summary["calibration"]
    lock = strat.get("lock_in")
    model = strat.get("model_edge")
    settled = int(t["settled_fills"])

    if settled < 10:
        return {"level": "early", "emoji": "⏳", "label": "Too early to judge",
                "detail": f"Only {settled} settled trade(s) so far — let it keep running. "
                          "Quiet, choppy days are normal; the picture forms over weeks."}

    if lock and lock["settled"] >= 5 and lock["hit_rate"] is not None and lock["hit_rate"] < 0.90:
        return {"level": "red", "emoji": "🔴", "label": "Investigate now",
                "detail": "The deterministic lock-in strategy is losing more than it should. "
                          "That usually means a data or settlement-station problem, not bad luck — "
                          "check the logs and re-run the mapping verification before trusting anything."}

    if t["net_usd"] < 0:
        return {"level": "red", "emoji": "🔴", "label": "Net negative",
                "detail": "The book is underwater after fees. See which strategy is dragging "
                          "(below) before drawing conclusions — one bad week isn't fatal."}

    concerns: list[str] = []
    if model and model["settled"] >= 5:
        if model["net_usd"] < 0:
            concerns.append("model-edge is unprofitable after fees")
        if cal["brier"] is not None and cal["brier"] > 0.24:
            concerns.append("model-edge predictions are barely better than a coin flip")
    if concerns:
        return {"level": "yellow", "emoji": "🟡", "label": "Working, with a watch item",
                "detail": "Net positive overall, but " + " and ".join(concerns) +
                          ". The deterministic side is carrying it; consider tightening or pausing "
                          "Strategy 2 (raise WA_MODEL_EDGE_THRESHOLD) if the calibration below stays poor."}

    return {"level": "green", "emoji": "🟢", "label": "On track",
            "detail": "Net positive and the deterministic strategy is healthy. If the calibration "
                      "below tracks the diagonal, the forecast edge is real too."}


def _interpretation(summary: dict[str, Any]) -> list[str]:
    t = summary["totals"]
    strat = summary["by_strategy"]
    cal = summary["calibration"]
    out: list[str] = []

    out.append(
        f"<b>Bottom line:</b> {_fmt_money(t['net_usd'])} net after "
        f"{_fmt_money(t['fees_usd'])} in estimated fees, across {t['settled_fills']} settled "
        f"trades ({t['unsettled_fills']} still open). Net — not gross — is the number that matters."
    )

    lock = strat.get("lock_in")
    if lock and lock["settled"]:
        health = "healthy" if (lock["hit_rate"] or 0) >= 0.95 else "LOWER THAN IT SHOULD BE"
        out.append(
            f"<b>Lock-in (deterministic):</b> won {int(lock['wins'])} of {int(lock['settled'])} "
            f"({_pct(lock['hit_rate'])}), net {_fmt_money(lock['net_usd'])}. This is your trust "
            f"anchor — it should sit near 100%. Right now it's {health}."
        )

    model = strat.get("model_edge")
    if model and model["settled"]:
        out.append(
            f"<b>Model-edge (forecast bets):</b> won {int(model['wins'])} of {int(model['settled'])} "
            f"({_pct(model['hit_rate'])}), net {_fmt_money(model['net_usd'])} after "
            f"{_fmt_money(model['fees_usd'])} fees. Lower hit-rate is expected; its value depends "
            f"entirely on the calibration check."
        )

    if cal["n"]:
        bins = cal["bins"]
        gap = sum((b["realized"] - b["mean_predicted"]) * b["n"] for b in bins) / max(1, cal["n"])
        if gap < -0.05:
            tone = ("The model is <b>overconfident</b> — its high-probability bets win less often "
                    "than it claims. Treat Strategy 2 with caution.")
        elif gap > 0.05:
            tone = ("The model is <b>under-confident</b> — bets win more often than predicted. "
                    "Real edge is likely being left on the table.")
        else:
            tone = "The model looks <b>well-calibrated</b> — predicted ≈ realized. That's the good case."
        brier = cal["brier"]
        brier_note = ("has genuine predictive signal" if brier is not None and brier < 0.20
                      else "is only marginally better than a coin flip" if brier is not None and brier < 0.25
                      else "is essentially a coin flip")
        out.append(f"<b>Calibration:</b> {tone} Brier score {brier:.3f} — the model {brier_note}.")

    gross = t["gross_usd"]
    if gross > 0:
        drag = t["fees_usd"] / gross
        out.append(
            f"<b>Fees:</b> ate {_pct(drag)} of gross profit — almost all from mid-book model-edge "
            f"trades (lock-in trades near 98¢ are barely taxed). This is why a nominal edge must "
            f"clear fees before a trade is taken."
        )
    return out


# --------------------------------------------------------------------------- #
# SVG charts (static, theme-aware via CSS vars)
# --------------------------------------------------------------------------- #
def _empty(msg: str) -> str:
    return f'<div class="wa-empty">{msg}</div>'


def _cumulative_svg(pnl_by_day: dict[str, float]) -> str:
    items = list(pnl_by_day.items())
    if not items:
        return _empty("No settled days yet — the P&L curve appears once markets settle.")
    cum = 0.0
    pts: list[tuple[str, float]] = []
    for day, v in items:
        cum += v
        pts.append((day, cum))

    W, H, pl, pr, pt, pb = 680, 240, 58, 14, 14, 30
    vals = [v for _, v in pts]
    ymin, ymax = min(0.0, min(vals)), max(0.0, max(vals))
    if ymax == ymin:
        ymax += 1.0
    n = len(pts)

    def X(i: int) -> float:
        return pl + (W - pl - pr) * (i / (n - 1) if n > 1 else 0.5)

    def Y(v: float) -> float:
        return pt + (H - pt - pb) * (1 - (v - ymin) / (ymax - ymin))

    zero = Y(0.0)
    line = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, (_, v) in enumerate(pts))
    area = (f"M{X(0):.1f},{zero:.1f} "
            + " ".join(f"L{X(i):.1f},{Y(v):.1f}" for i, (_, v) in enumerate(pts))
            + f" L{X(n - 1):.1f},{zero:.1f} Z")
    ex, ey = X(n - 1), Y(pts[-1][1])
    final = pts[-1][1]
    end_color = _GOOD if final >= 0 else _CRIT

    grid = (f'<line x1="{pl}" y1="{zero:.1f}" x2="{W - pr}" y2="{zero:.1f}" class="wa-axis"/>'
            f'<text x="{pl - 8}" y="{Y(ymax) + 4:.1f}" class="wa-ytick" text-anchor="end">{_fmt_money(ymax)}</text>'
            f'<text x="{pl - 8}" y="{zero + 4:.1f}" class="wa-ytick" text-anchor="end">$0</text>'
            f'<text x="{pl - 8}" y="{Y(ymin) + 4:.1f}" class="wa-ytick" text-anchor="end">{_fmt_money(ymin)}</text>')
    xlabels = (f'<text x="{X(0):.1f}" y="{H - 8}" class="wa-xtick" text-anchor="start">{pts[0][0]}</text>'
               f'<text x="{X(n - 1):.1f}" y="{H - 8}" class="wa-xtick" text-anchor="end">{pts[-1][0]}</text>')
    return (
        f'<svg viewBox="0 0 {W} {H}" class="wa-chart" role="img" '
        f'aria-label="Cumulative net profit and loss over time, ending at {_fmt_money(final)}">'
        f'<path d="{area}" fill="var(--accent)" fill-opacity="0.12"/>'
        f'{grid}'
        f'<polyline points="{line}" fill="none" stroke="var(--accent)" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="4.5" fill="{end_color}" '
        f'stroke="var(--surface)" stroke-width="2"/>'
        f'{xlabels}</svg>'
    )


def _perday_svg(pnl_by_day: dict[str, float]) -> str:
    items = list(pnl_by_day.items())
    if not items:
        return _empty("No settled days yet.")
    W, H, pl, pr, pt, pb = 680, 200, 58, 14, 12, 26
    vals = [v for _, v in items]
    ymin, ymax = min(0.0, min(vals)), max(0.0, max(vals))
    span = (ymax - ymin) or 1.0
    n = len(items)
    slot = (W - pl - pr) / n
    bw = min(26.0, slot * 0.68)

    def Y(v: float) -> float:
        return pt + (H - pt - pb) * ((ymax - v) / span)

    zero = Y(0.0)
    bars = []
    for i, (_, v) in enumerate(items):
        cx = pl + slot * (i + 0.5)
        y = Y(v)
        top, height = (min(y, zero), abs(zero - y))
        color = _GOOD if v >= 0 else _CRIT
        bars.append(
            f'<rect x="{cx - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" '
            f'height="{max(1.0, height):.1f}" rx="2" fill="{color}"/>'
        )
    grid = (f'<line x1="{pl}" y1="{zero:.1f}" x2="{W - pr}" y2="{zero:.1f}" class="wa-axis"/>'
            f'<text x="{pl - 8}" y="{Y(ymax) + 4:.1f}" class="wa-ytick" text-anchor="end">{_fmt_money(ymax)}</text>'
            f'<text x="{pl - 8}" y="{Y(ymin) + 4:.1f}" class="wa-ytick" text-anchor="end">{_fmt_money(ymin)}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" class="wa-chart" role="img" '
            f'aria-label="Net profit and loss for each settled day; green bars are up days, red bars down days">'
            f'{grid}{"".join(bars)}</svg>')


def _calibration_svg(cal: dict[str, Any]) -> str:
    bins = cal["bins"]
    if not bins:
        return _empty("No settled model-edge trades yet — this diagram fills in as forecast bets settle.")
    S, pad = 300, 40

    def X(p: float) -> float:
        return pad + (S - 2 * pad) * p

    def Y(p: float) -> float:
        return (S - pad) - (S - 2 * pad) * p

    grid = []
    for g in (0.0, 0.5, 1.0):
        grid.append(f'<line x1="{X(g):.1f}" y1="{Y(0):.1f}" x2="{X(g):.1f}" y2="{Y(1):.1f}" class="wa-grid"/>')
        grid.append(f'<line x1="{X(0):.1f}" y1="{Y(g):.1f}" x2="{X(1):.1f}" y2="{Y(g):.1f}" class="wa-grid"/>')
    diag = f'<line x1="{X(0):.1f}" y1="{Y(0):.1f}" x2="{X(1):.1f}" y2="{Y(1):.1f}" class="wa-diag"/>'
    nmax = max(b["n"] for b in bins)
    dots = []
    for b in bins:
        r = 5 + 7 * (b["n"] / nmax)
        cx, cy = X(b["mean_predicted"]), Y(b["realized"])
        dots.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="var(--accent)" '
                    f'fill-opacity="0.85" stroke="var(--surface)" stroke-width="1.5"/>')
    labels = (
        f'<text x="{X(0.5):.1f}" y="{S - 8}" class="wa-xtick" text-anchor="middle">predicted P(win) →</text>'
        f'<text x="14" y="{Y(0.5):.1f}" class="wa-xtick" text-anchor="middle" '
        f'transform="rotate(-90 14 {Y(0.5):.1f})">realized win rate →</text>'
        f'<text x="{X(0):.1f}" y="{S - pad + 16:.1f}" class="wa-xtick" text-anchor="middle">0%</text>'
        f'<text x="{X(1):.1f}" y="{S - pad + 16:.1f}" class="wa-xtick" text-anchor="middle">100%</text>'
    )
    return (f'<svg viewBox="0 0 {S} {S}" class="wa-chart wa-square" role="img" '
            f'aria-label="Calibration: predicted win probability versus realized win rate. '
            f'Points on the diagonal are well calibrated; below it means overconfident.">'
            f'{"".join(grid)}{diag}{"".join(dots)}{labels}</svg>')


# --------------------------------------------------------------------------- #
# Page assembly
# --------------------------------------------------------------------------- #
def _stat_tiles(summary: dict[str, Any]) -> str:
    t = summary["totals"]
    cal = summary["calibration"]
    lock = summary["by_strategy"].get("lock_in", {})
    net = t["net_usd"]
    net_cls = "pos" if net >= 0 else "neg"
    lock_hit = lock.get("hit_rate")
    brier = cal["brier"]
    tiles = [
        ("Net P&amp;L (after fees)", _fmt_money(net), net_cls,
         f"gross {_fmt_money(t['gross_usd'])} · fees {_fmt_money(t['fees_usd'])}"),
        ("Lock-in hit rate", _pct(lock_hit) if lock_hit is not None else "—",
         "pos" if (lock_hit or 0) >= 0.95 else ("neg" if lock_hit is not None else "mut"),
         "should be near 100%"),
        ("Model calibration (Brier)", f"{brier:.3f}" if brier is not None else "—",
         "pos" if (brier is not None and brier < 0.20) else ("neg" if (brier is not None and brier > 0.24) else "mut"),
         "lower is better · 0.25 = coin flip"),
        ("Settled / open", f"{t['settled_fills']} / {t['unsettled_fills']}", "mut",
         f"{t['settled_markets']} markets settled"),
    ]
    cells = "".join(
        f'<div class="wa-tile"><div class="wa-tile-k">{k}</div>'
        f'<div class="wa-tile-v {cls}">{v}</div><div class="wa-tile-s">{s}</div></div>'
        for k, v, cls, s in tiles
    )
    return f'<div class="wa-tiles">{cells}</div>'


def _strategy_table(summary: dict[str, Any]) -> str:
    rows = []
    labels = {"lock_in": "Lock-in (deterministic)", "model_edge": "Model-edge (forecast)",
              "manual": "Manual"}
    for strat, v in summary["by_strategy"].items():
        net = v["net_usd"]
        cls = "pos" if net >= 0 else "neg"
        rows.append(
            f"<tr><td>{labels.get(strat, strat)}</td>"
            f"<td class='num'>{int(v['fills'])}</td>"
            f"<td class='num'>{int(v['settled'])}</td>"
            f"<td class='num'>{_pct(v['hit_rate'], 1) if v['hit_rate'] is not None else '—'}</td>"
            f"<td class='num'>{_fmt_money(v['gross_usd'])}</td>"
            f"<td class='num'>{_fmt_money(v['fees_usd'])}</td>"
            f"<td class='num {cls}'>{_fmt_money(net)}</td></tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='7' class='mut'>No fills yet.</td></tr>")
    return (
        "<table class='wa-table'><thead><tr>"
        "<th>Strategy</th><th class='num'>Fills</th><th class='num'>Settled</th>"
        "<th class='num'>Hit</th><th class='num'>Gross</th><th class='num'>Fees</th>"
        "<th class='num'>Net</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table>"
    )


_PLAN = [
    ("Week 1 — let it settle in",
     ["Confirm it's alive: <code>tail logs/tracker.out.log</code> should show a "
      "<b>heartbeat</b> line every ~10 min with <code>markets_tracked</code> &gt; 0.",
      "Run <code>python -m weather_arb.verify_mappings</code> once — expect ALL CONFIRMED.",
      "Then leave it alone. Few or no trades and red days are normal this early."]),
    ("Weeks 2–3 — a 5-minute weekly check",
     ["Regenerate this page: <code>python -m weather_arb.report --html &amp;&amp; open report.html</code>.",
      "Glance at the verdict light and the cumulative curve — is the trend up?",
      "Confirm the <b>lock-in hit rate</b> is still near 100% (that's the plumbing working).",
      "Skim <code>logs/verify.log</code> — any day that isn't ALL CONFIRMED means a series "
      "auto-paused; re-verify the mapping."]),
    ("Week 4 — decide",
     ["Read the verdict and the calibration diagram together.",
      "Apply the decision table above: keep, tighten Strategy 2, or investigate.",
      "If green and calibration tracks the diagonal, consider a small live test on Kalshi's "
      "<b>demo</b> endpoints before anything real."]),
]


def _plan_html() -> str:
    blocks = []
    for title, items in _PLAN:
        lis = "".join(f"<li>{it}</li>" for it in items)
        blocks.append(f'<div class="wa-step"><h3>{title}</h3><ul>{lis}</ul></div>')
    return f'<div class="wa-plan">{"".join(blocks)}</div>'


_DECISION_ROWS = [
    ("🟢", "green", "Net up, lock-in ~100%, calibration on the diagonal",
     "Working. Keep running; consider a demo-endpoint live test."),
    ("🟡", "yellow", "Net up but only from lock-in; model-edge flat/negative or poorly calibrated",
     "Keep lock-in. Raise <code>WA_MODEL_EDGE_THRESHOLD</code> or pause Strategy 2."),
    ("🔴", "red", "Net negative, or lock-in hit-rate dropping",
     "Stop &amp; investigate — losing on the deterministic side points to a data/mapping issue, not luck."),
]


def _decision_table() -> str:
    rows = "".join(
        f'<tr><td class="wa-dot {cls}">{emoji}</td><td>{cond}</td><td>{act}</td></tr>'
        for emoji, cls, cond, act in _DECISION_ROWS
    )
    return (f'<table class="wa-table wa-decide"><thead><tr><th></th>'
            f'<th>What you see</th><th>What to do</th></tr></thead><tbody>{rows}</tbody></table>')


_CSS = """
:root{
  --plane:#f6f7f9; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --mut:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10);
  --accent:#2a78d6; --good:#0ca30c; --crit:#d03b3b; --warn:#fab219;
  --good-bg:rgba(12,163,12,.10); --crit-bg:rgba(208,59,59,.10); --warn-bg:rgba(250,178,25,.14);
}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){
  --plane:#0c0d0f; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --mut:#898781;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
  --accent:#3987e5; --good:#0ca30c; --crit:#e66767; --warn:#fab219;
  --good-bg:rgba(12,163,12,.14); --crit-bg:rgba(230,103,103,.14); --warn-bg:rgba(250,178,25,.16);
}}
:root[data-theme="dark"]{
  --plane:#0c0d0f; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --mut:#898781;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
  --accent:#3987e5; --good:#0ca30c; --crit:#e66767; --warn:#fab219;
  --good-bg:rgba(12,163,12,.14); --crit-bg:rgba(230,103,103,.14); --warn-bg:rgba(250,178,25,.16);
}
*{box-sizing:border-box}
.wa-page{background:var(--plane);color:var(--ink);
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.5;
  padding:32px 20px 64px;min-height:100%;}
.wa-wrap{max-width:940px;margin:0 auto;display:flex;flex-direction:column;gap:22px}
.wa-head h1{font-size:1.5rem;font-weight:700;margin:0;letter-spacing:-.01em;text-wrap:balance}
.wa-prov{color:var(--ink2);font-size:.85rem;margin-top:4px}
.wa-example{display:inline-block;margin-top:8px;padding:3px 10px;border-radius:999px;
  background:var(--warn-bg);color:var(--ink);font-size:.72rem;font-weight:600;
  letter-spacing:.03em;text-transform:uppercase}
.wa-card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px 22px}
.wa-card h2{font-size:.78rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
  color:var(--mut);margin:0 0 14px}
.wa-verdict{display:flex;gap:16px;align-items:flex-start;border-radius:14px;padding:20px 22px;
  border:1px solid var(--border)}
.wa-verdict.green{background:var(--good-bg)} .wa-verdict.yellow{background:var(--warn-bg)}
.wa-verdict.red{background:var(--crit-bg)} .wa-verdict.early{background:var(--surface)}
.wa-vemoji{font-size:2rem;line-height:1}
.wa-vlabel{font-size:1.15rem;font-weight:700;margin:0 0 4px}
.wa-vdetail{color:var(--ink2);font-size:.92rem;margin:0;max-width:64ch}
.wa-tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
.wa-tile{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:16px 18px}
.wa-tile-k{font-size:.74rem;color:var(--mut);font-weight:600;letter-spacing:.02em}
.wa-tile-v{font-size:1.7rem;font-weight:700;margin:6px 0 2px;font-variant-numeric:tabular-nums}
.wa-tile-s{font-size:.74rem;color:var(--mut)}
.pos{color:var(--good)} .neg{color:var(--crit)} .mut{color:var(--mut)}
.wa-grid2{display:grid;grid-template-columns:1.4fr 1fr;gap:22px;align-items:start}
.wa-chart{width:100%;height:auto;display:block}
.wa-square{max-width:340px;margin:0 auto}
.wa-axis{stroke:var(--axis);stroke-width:1}
.wa-grid{stroke:var(--grid);stroke-width:1}
.wa-diag{stroke:var(--mut);stroke-width:1.5;stroke-dasharray:4 4}
.wa-ytick,.wa-xtick{fill:var(--mut);font-size:11px;font-family:system-ui,sans-serif;font-variant-numeric:tabular-nums}
.wa-empty{color:var(--mut);font-size:.9rem;padding:28px 4px;text-align:center}
.wa-interp{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:12px}
.wa-interp li{padding-left:16px;border-left:3px solid var(--accent);color:var(--ink2);font-size:.94rem}
.wa-interp b{color:var(--ink)}
.wa-table{width:100%;border-collapse:collapse;font-size:.9rem}
.wa-table th,.wa-table td{padding:9px 10px;text-align:left;border-bottom:1px solid var(--border)}
.wa-table th{color:var(--mut);font-weight:600;font-size:.76rem;letter-spacing:.03em;text-transform:uppercase}
.wa-table td.num,.wa-table th.num{text-align:right;font-variant-numeric:tabular-nums}
.wa-decide td{vertical-align:top} .wa-dot{font-size:1.1rem;text-align:center;width:34px}
.wa-caption{color:var(--mut);font-size:.8rem;margin:10px 2px 0}
.wa-plan{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}
.wa-step h3{font-size:.92rem;font-weight:700;margin:0 0 8px}
.wa-step ul{margin:0;padding-left:18px;display:flex;flex-direction:column;gap:7px}
.wa-step li{font-size:.86rem;color:var(--ink2)}
code{background:var(--plane);border:1px solid var(--border);border-radius:5px;
  padding:1px 5px;font-size:.85em;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.wa-foot{color:var(--mut);font-size:.8rem;text-align:center;margin-top:6px}
@media (max-width:760px){
  .wa-tiles{grid-template-columns:repeat(2,1fr)} .wa-grid2{grid-template-columns:1fr}
  .wa-plan{grid-template-columns:1fr}
}
"""


def render_html(
    summary: dict[str, Any],
    *,
    source_label: str,
    is_example: bool = False,
    standalone: bool = True,
) -> str:
    """Return the dashboard HTML. ``standalone`` wraps a full document (for a
    local file); otherwise returns body-only markup (for an Artifact)."""
    v = _verdict(summary)
    example_badge = ('<span class="wa-example">Example — simulated data</span>'
                     if is_example else "")
    interp = "".join(f"<li>{b}</li>" for b in _interpretation(summary))

    body = f"""
<div class="wa-page"><div class="wa-wrap">
  <header class="wa-head">
    <h1>Weather-arb — performance &amp; 4-week guide</h1>
    <div class="wa-prov">Ledger: {source_label}</div>
    {example_badge}
  </header>

  <section class="wa-verdict {v['level']}">
    <div class="wa-vemoji">{v['emoji']}</div>
    <div><p class="wa-vlabel">{v['label']}</p><p class="wa-vdetail">{v['detail']}</p></div>
  </section>

  {_stat_tiles(summary)}

  <section class="wa-card">
    <h2>Cumulative net P&amp;L</h2>
    {_cumulative_svg(summary['pnl_by_day'])}
    <p class="wa-caption">Money in your pocket over time, after fees. Watch the trend, not any single day.</p>
  </section>

  <div class="wa-grid2">
    <section class="wa-card">
      <h2>Net P&amp;L by day</h2>
      {_perday_svg(summary['pnl_by_day'])}
      <p class="wa-caption">Green = up day, red = down day. Choppiness is normal.</p>
    </section>
    <section class="wa-card">
      <h2>Model-edge calibration</h2>
      {_calibration_svg(summary['calibration'])}
      <p class="wa-caption">Dots on the dashed line = forecast was right as often as it claimed.
        Below the line = overconfident. Dot size = number of bets.</p>
    </section>
  </div>

  <section class="wa-card">
    <h2>What your numbers mean</h2>
    <ul class="wa-interp">{interp}</ul>
  </section>

  <section class="wa-card">
    <h2>By strategy</h2>
    {_strategy_table(summary)}
  </section>

  <section class="wa-card">
    <h2>Decide at a glance</h2>
    {_decision_table()}
  </section>

  <section class="wa-card">
    <h2>Your 4-week plan</h2>
    {_plan_html()}
  </section>

  <p class="wa-foot">Regenerate anytime: <code>python -m weather_arb.report --html</code> ·
    all figures are net of estimated fees.</p>
</div></div>
"""
    style = f"<style>{_CSS}</style>"
    if not standalone:
        return style + body
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>weather-arb performance</title>" + style +
        "</head><body>" + body + "</body></html>"
    )
