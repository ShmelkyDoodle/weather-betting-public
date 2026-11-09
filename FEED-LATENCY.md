# Can we reach the observation-latency edge? — ANSWERED 2026-09-08: **no**

**The latency edge is dead, and not for the reason anyone expected. The
sub-hourly observation stream reports WHOLE DEGREES CELSIUS, so it barely leads
the hourly METAR at all — and at the moment it does lead, the sure side is
already quoted at 100¢ in 505 of 505 cases.**

Reproduce:

    PYTHONPATH=. .venv/bin/python scripts/t1_price_history.py --days 45
    PYTHONPATH=. .venv/bin/python scripts/t1_depth_probe.py --once --verbose
    PYTHONPATH=. .venv/bin/python tests/test_v40.py

Read-only. No tracker change, no schema change, no cron job, no launch agent;
`main` still at `f757af7`.

## THE RESULT (OBSERVED, n=539 certainty events, 5 cities, 45 days)

      wrong calls vs official settlement        0 / 539
      usable lead after charging feed latency   median 0 min (zero on 308/516)
      hourly METAR never proves it              23 / 539

      AT t1 (before the hourly METAR -- the claimed edge)
        priced at exactly 100c                  505 / 505   = 100%
        below the 98c lock-in ceiling                 0     =   0%

      AT t2 (hourly METAR lands -- the market knows)
        priced at exactly 100c                  453 / 507   =  89%
        below the 98c ceiling                    35 / 507   =   7%  (median 91c)

**There is nothing to buy at t1.** Not "thin", not "shallow" — every single
quoted sure side is at 100¢. And t2, when the market already knows, is *better*
(7% tradeable), which is consistent with the existing Strategy-1 result of ~1
tradeable opportunity per 30 days.

## ⚠⚠ WHY, AND THIS IS THE FINDING WORTH KEEPING

**The 5-minute MADIS T-group is whole degrees Celsius padded to look like
tenths.** Measured over **17,007** reports at 4 stations:

      channel        n       T-groups ending in .0
      sub-hourly   17,007            100.0%
      routine       1,436          8.1 - 12.5%   (i.e. chance)

So a sub-hourly `T0310` does **not** mean 31.0 °C ± 0.1. It means the true
temperature is somewhere in **[30.5, 31.5) °C = [86.9, 88.7) °F** — a 1.8 °F
band. To prove a whole-degree bucket crossing you need a full degree Celsius of
headroom past the boundary, which mostly arrives no earlier than the hourly
METAR does.

**Treating it as exact manufactures certainty that is not there**, and it is
measurable: before the fix, of 18 lock-ins that ONLY the sub-hourly channel
proved, **4 were wrong** against official settlement (22%) — while all 111 that
the routine METAR also proved were correct. After applying the correct lower
bound: **0 wrong in 539**. A wrong lock-in loses the entire contract.

⚠ **This corrects an earlier claim in this very file.** An intermediate pass here
reported that IEM `asos.py` carries "full T-group precision" and proves the
settled max on 56/60 station-days, giving a ~15-minute net window. **That was
wrong** — it read the T-group as genuine tenths. Both real-time routes are 1.8 °F
because the coarseness is in the underlying MADIS report, not in either
distributor. The corrected lead is ~0 minutes.

## WHAT WAS RIGHT, AND STILL IS

- **The recorded blocker was indeed wrong about the endpoint.** `asos1min.py` is
  ~2 days stale; **`asos.py`** carries the 5-minute reports at a measured
  ~18 min. That part stands — it just doesn't help, because of the precision.
- **Publication latency, measured by first-sighting** (poll every 60 s, record
  when each observation first becomes *visible* — not by sampling how old the
  newest row is; conflating those is the error `f757af7` warns about):

      weather.gov / Synoptic    8.8 min (p90 9.5)   -- also truncated before RMK
      IEM asos.py              18.0 min (p25 12, p75 23)
      aviationweather.gov      ~1 min, but HOURLY ONLY -- no sub-hourly rows

- **Coverage is 5 of 7 cities anyway**: KNYC has ~12 sub-hourly rows/day and
  **KDEN has none at all**.

## THE DEPTH PROBE IS NOW MOOT — KEPT ANYWAY

`scripts/t1_depth_probe.py` was built to answer "is there depth at t1?". The
price result makes it unnecessary: at 100¢ there is no trade to size. It is kept
because it is the only instrument that samples the live book at the moment
certainty appears, and its same-instant control is independently useful — it
showed undecided markets in the same event ladder at median depth **878** while
already-certain ones sat at **0 on 12/12**, which is direct evidence that
**certainty specifically empties the sure side** rather than the venue being
illiquid.

## THE GENERAL LESSON

Certainty is not information. Markets price *probability*, not *proof*. By the
time an outcome is formally provable it has been overwhelmingly likely for a
while and is priced accordingly. That is why the sure side is at 100¢ before we
can prove anything, and it is why no faster feed fixes this: the gap this thesis
needed — between "provable" and "already priced" — is approximately zero.

**This closes the last open line of attack in the project.** Every avenue has now
been measured: Strategy 2 (structurally dead), fading it, passive placement,
model choice, entry timing, and now observation latency.
