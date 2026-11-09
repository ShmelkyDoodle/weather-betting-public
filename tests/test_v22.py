"""#1 intraday model wired into the live forecast: blend math, non-blocking curve
cache, `_id` shadow variants, and the opt-in trading path (offline)."""
import asyncio, logging, os, sys, tempfile
from datetime import datetime, timedelta, timezone
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb.config import Settings, StationConfig
from weather_arb.db import Database
from weather_arb.forecast_ingest import ForecastClient, base_model_name
from weather_arb.intraday import IntradayCurves, Prediction, blend_normal
from weather_arb.market_discovery import MarketRegistry
from weather_arb.arb_engine import ArbitrageEngine
from weather_arb.models import Venue, WeatherMarket, STRIKE_BETWEEN
from weather_arb.risk_manager import RiskManager
from weather_arb import timeutils

logging.disable(logging.CRITICAL)   # the failure paths log tracebacks on purpose

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)

STA = StationConfig("KMDW", "Chicago Midway", 41.786, -87.752, -6)   # CST
# Interior bucket 85-87F. An interior bucket is where sigma actually matters: a
# tail-high bucket the observed max has already cleared is a Strategy-1 lock-in
# (P=1 either way), so it could never show the sharpening effect.
MKT = WeatherMarket("KXHIGHCHI-D-B86", Venue.KALSHI, "KMDW", floor_strike_f=85.0,
                    cap_strike_f=87.0, strike_type=STRIKE_BETWEEN)


# --------------------------------------------------------------------------- #
# blend_normal
# --------------------------------------------------------------------------- #
def test_blend_precision_weighting():
    # Morning: the intraday remaining-rise spread is wider than the forecast's,
    # so the forecast should carry the blended mean.
    mu, sigma = blend_normal(88.0, 4.5, 92.0, 8.0)
    check("wide intraday -> mean stays near the forecast", 88.0 <= mu < 89.5)
    check("sigma = the sharper of the two (forecast)", abs(sigma - 4.5) < 1e-9)

    # After peak: remaining rise has collapsed, so the observation takes over.
    mu2, sigma2 = blend_normal(88.0, 4.5, 91.0, 0.3, sigma_floor=0.1)
    check("sharp intraday -> mean pulled onto the observation", mu2 > 90.9)
    check("sigma collapses to the intraday sigma", abs(sigma2 - 0.3) < 1e-9)


def test_blend_never_sharper_than_either_input():
    # The Bayesian product rule would give sqrt(1/(1/4+1/4)) ~= 1.41 here; we
    # deliberately refuse that unearned narrowing (the two errors are correlated).
    _, sigma = blend_normal(80.0, 2.0, 84.0, 2.0, sigma_floor=0.1)
    check("equal sigmas -> sigma unchanged, not product-rule shrunk", abs(sigma - 2.0) < 1e-9)
    mu, _ = blend_normal(80.0, 2.0, 84.0, 2.0, sigma_floor=0.1)
    check("equal sigmas -> mean is the midpoint", abs(mu - 82.0) < 1e-9)


def test_blend_sigma_floor_and_degenerate_inputs():
    _, sigma = blend_normal(88.0, 4.5, 90.0, 0.05, sigma_floor=1.0)
    check("sigma floored", abs(sigma - 1.0) < 1e-9)
    # A zero sigma must not divide by zero; it just wins the weighting outright.
    mu, sigma2 = blend_normal(88.0, 4.5, 90.0, 0.0, sigma_floor=0.5)
    check("zero sigma survives without ZeroDivisionError", abs(mu - 90.0) < 1e-6)
    check("zero sigma still floored", abs(sigma2 - 0.5) < 1e-9)


# --------------------------------------------------------------------------- #
# IntradayCurves: caching, non-blocking build, failure backoff
# --------------------------------------------------------------------------- #
class StubModel:
    """Stands in for IntradayModel's IEM fetching. Counts builds; can fail."""
    def __init__(self, curve, fail=False):
        self.curve_value, self.fail, self.calls = curve, fail, 0

    async def curve(self, icao, month, day, offset):
        self.calls += 1
        await asyncio.sleep(0)              # yield, like a real fetch would
        if self.fail:
            raise RuntimeError("IEM down")
        return self.curve_value


# Hour 12 LST: 8F still to come (spread 3F). Hour 16 LST: the max is in.
CURVE = {12: (8.0, 3.0, 200), 16: (0.1, 0.4, 200)}


def _now_at_lst(hour):
    """A UTC 'now' that is ``hour`` LST at KMDW (UTC-6), on 2026-07-22."""
    return datetime(2026, 7, 22, hour, tzinfo=timezone.utc) + timedelta(hours=6)


def _curves(curve=CURVE, fail=False, wait=False):
    c = IntradayCurves(session=None, settings=Settings(), wait_for_build=wait)
    c._model = StubModel(curve, fail)      # type: ignore[assignment]
    return c


async def test_lookup_is_non_blocking_then_cached():
    c = _curves()
    now = _now_at_lst(12)
    cd = timeutils.climate_date_str(now, -6)
    first = await c.prediction(station=STA, climate_date=cd, observed_tmax_f=80.0, now=now)
    check("first lookup returns None instead of blocking on the archive", first is None)
    await asyncio.gather(*c._builds.values())          # let the background build land
    second = await c.prediction(station=STA, climate_date=cd, observed_tmax_f=80.0, now=now)
    check("second lookup uses the cached curve", second is not None)
    check("prediction = running max + mean rise", abs(second.predicted_max_f - 88.0) < 1e-9)
    check("sigma from the curve spread", abs(second.sigma_f - 3.0) < 1e-9)
    check("curve built exactly once", c._model.calls == 1)


async def test_uncertainty_collapses_after_peak():
    c = _curves()
    now = _now_at_lst(16)
    cd = timeutils.climate_date_str(now, -6)
    await c.prediction(station=STA, climate_date=cd, observed_tmax_f=88.0, now=now)
    await asyncio.gather(*c._builds.values())
    p = await c.prediction(station=STA, climate_date=cd, observed_tmax_f=88.0, now=now)
    check("post-peak max is essentially the running max", p is not None and p.predicted_max_f < 88.2)
    check("post-peak sigma floored at the configured 1.0F", abs(p.sigma_f - 1.0) < 1e-9)


async def test_not_applicable_cases_return_none():
    c = _curves()
    now = _now_at_lst(12)
    cd = timeutils.climate_date_str(now, -6)
    check("no observation yet -> None",
          await c.prediction(station=STA, climate_date=cd, observed_tmax_f=None, now=now) is None)
    check("no build scheduled without an observation", not c._builds)
    check("a different climate day -> None",
          await c.prediction(station=STA, climate_date="2026-07-25",
                             observed_tmax_f=80.0, now=now) is None)
    check("no build scheduled for another day", not c._builds)
    # An hour the curve has no history for yields no prediction (curve is 12/16 only).
    c2 = _curves()
    early = _now_at_lst(3)
    cd2 = timeutils.climate_date_str(early, -6)
    await c2.prediction(station=STA, climate_date=cd2, observed_tmax_f=70.0, now=early)
    await asyncio.gather(*c2._builds.values())
    check("hour missing from the curve -> None",
          await c2.prediction(station=STA, climate_date=cd2, observed_tmax_f=70.0, now=early) is None)


async def test_failed_build_backs_off_and_degrades():
    c = _curves(fail=True)
    now = _now_at_lst(12)
    cd = timeutils.climate_date_str(now, -6)
    await c.prediction(station=STA, climate_date=cd, observed_tmax_f=80.0, now=now)
    await asyncio.gather(*c._builds.values(), return_exceptions=True)
    check("a failed build does not raise into the engine",
          await c.prediction(station=STA, climate_date=cd, observed_tmax_f=80.0, now=now) is None)
    for _ in range(5):
        await c.prediction(station=STA, climate_date=cd, observed_tmax_f=80.0, now=now)
    check("failure backoff prevents a retry storm", c._model.calls == 1)

    # Too little history is treated the same way (no curve, backed off).
    empty = _curves(curve={})
    await empty.prediction(station=STA, climate_date=cd, observed_tmax_f=80.0, now=now)
    await asyncio.gather(*empty._builds.values())
    await empty.prediction(station=STA, climate_date=cd, observed_tmax_f=80.0, now=now)
    check("empty curve backs off too", empty._model.calls == 1)


async def test_wait_for_build_mode():
    c = _curves(wait=True)
    now = _now_at_lst(12)
    cd = timeutils.climate_date_str(now, -6)
    p = await c.prediction(station=STA, climate_date=cd, observed_tmax_f=80.0, now=now)
    check("wait_for_build gets a prediction on the first lookup (signals CLI)", p is not None)


# --------------------------------------------------------------------------- #
# ForecastClient: _id shadow variants + the sharpened fast path
# --------------------------------------------------------------------------- #
class StubIntraday:
    def __init__(self, prediction=None, raises=False):
        self._p, self._raises = prediction, raises

    async def prediction(self, *, station, climate_date, observed_tmax_f, now=None):
        if self._raises:
            raise RuntimeError("boom")
        return self._p


def _client(intraday=None):
    fc = ForecastClient(session=None, settings=Settings(), mos=None, intraday=intraday)
    async def fake_grid(s, c): return (88.0, 4.5)
    fc._grid_forecast_high = fake_grid     # type: ignore[assignment]
    return fc


SHARP = Prediction(predicted_max_f=86.0, sigma_f=0.5, mean_rise_f=0.0, n=200)


async def test_id_variants_emitted():
    fc = _client(StubIntraday(SHARP))
    ests = await fc.model_probabilities(station=STA, market=MKT,
                                        climate_date="2026-07-22", observed_tmax_f=86.0)
    check("current_id emitted alongside current", "current" in ests and "current_id" in ests)
    check("standalone intraday model emitted", "intraday" in ests)
    check("base model keeps its lead-time sigma", abs(ests["current"].sigma_f - 4.5) < 1e-9)
    check("sharpened variant has a much smaller sigma", ests["current_id"].sigma_f <= 1.0)
    check("intraday model uses the intraday mean", abs(ests["intraday"].forecast_high_f - 86.0) < 1e-9)
    # 86F observed, bucket is >=85F: sharpening should make YES near-certain, and
    # it must be *more* confident than the smeared lead-time forecast.
    check("sharpened P(YES) exceeds the unsharpened one",
          ests["current_id"].probability > ests["current"].probability)


async def test_no_intraday_client_or_no_curve_changes_nothing():
    plain = await _client(None).model_probabilities(
        station=STA, market=MKT, climate_date="2026-07-22", observed_tmax_f=86.0)
    check("no intraday client -> no _id variants", not any(k.endswith("_id") for k in plain)
          and "intraday" not in plain)
    none_yet = await _client(StubIntraday(None)).model_probabilities(
        station=STA, market=MKT, climate_date="2026-07-22", observed_tmax_f=86.0)
    check("curve not ready -> no _id variants", not any(k.endswith("_id") for k in none_yet))
    check("base models still produced", "current" in none_yet)


async def test_intraday_failure_never_breaks_forecasting():
    ests = await _client(StubIntraday(raises=True)).model_probabilities(
        station=STA, market=MKT, climate_date="2026-07-22", observed_tmax_f=86.0)
    check("a raising intraday client degrades to the plain forecast", "current" in ests)
    check("and emits no _id variants", not any(k.endswith("_id") for k in ests))


async def test_market_probability_fast_path_sharpening():
    fc = _client(StubIntraday(SHARP))
    base = await fc.market_probability(station=STA, market=MKT,
                                       climate_date="2026-07-22", observed_tmax_f=86.0)
    sharp = await fc.market_probability(station=STA, market=MKT, climate_date="2026-07-22",
                                        observed_tmax_f=86.0, apply_intraday=True)
    check("fast path defaults to unsharpened", abs(base.sigma_f - 4.5) < 1e-9)
    check("apply_intraday sharpens the fast path", sharp.sigma_f <= 1.0)


# --------------------------------------------------------------------------- #
# ArbitrageEngine: the opt-in flag actually gates the trading path
# --------------------------------------------------------------------------- #
def _engine(settings, db, forecast=None):
    return ArbitrageEngine(settings=settings, db=db, client=None,
                           risk=RiskManager(settings=settings, db=db),
                           forecast=forecast, registry=MarketRegistry())


async def _seed_id_winner(db, n=6):
    """'current_id' predicts correctly, 'current' inverted -> _id wins on Brier."""
    for i in range(n):
        tk = f"T{i}"
        res = "yes" if i % 2 == 0 else "no"
        pyes = 0.9 if res == "yes" else 0.1
        await db.record_model_prediction(ticker=tk, model="current_id", climate_date="2026-07-10",
                                         prob=pyes, forecast_high_f=90, sigma_f=1)
        await db.record_model_prediction(ticker=tk, model="current", climate_date="2026-07-10",
                                         prob=1 - pyes, forecast_high_f=90, sigma_f=4)
        await db.record_market_result(tk, res)


def test_base_model_name():
    check("_id suffix stripped", base_model_name("ensemble_id") == "ensemble")
    check("standalone intraday is gated too (no base of its own)",
          base_model_name("intraday") == "current")
    check("plain models pass through", base_model_name("nbm") == "nbm")
    check("_bc variants are untouched", base_model_name("nbm_bc") == "nbm_bc")


async def test_auto_selection_respects_the_flag():
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "s.sqlite3")) as db:
            await _seed_id_winner(db)
            off = _engine(Settings(trading_model="auto", model_select_min_samples=5,
                                   enable_intraday_forecast=False), db)
            check("flag off -> a winning _id pick falls back to its base model",
                  await off._selected_model_name() == "current")
            on = _engine(Settings(trading_model="auto", model_select_min_samples=5,
                                  enable_intraday_forecast=True), db)
            check("flag on -> the _id winner is traded",
                  await on._selected_model_name() == "current_id")


async def test_pinned_intraday_model_is_gated_too():
    """A pinned/auto-picked standalone `intraday` must also answer to the flag --
    it is just as curve-derived as the `_id` blends."""
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "s.sqlite3")) as db:
            for i in range(6):
                res = "yes" if i % 2 == 0 else "no"
                await db.record_model_prediction(ticker=f"T{i}", model="intraday",
                                                 climate_date="2026-07-10",
                                                 prob=0.9 if res == "yes" else 0.1,
                                                 forecast_high_f=90, sigma_f=1)
                await db.record_market_result(f"T{i}", res)
            off = _engine(Settings(trading_model="auto", model_select_min_samples=5,
                                   enable_intraday_forecast=False), db)
            check("flag off -> a winning 'intraday' pick falls back to current",
                  await off._selected_model_name() == "current")
            on = _engine(Settings(trading_model="auto", model_select_min_samples=5,
                                  enable_intraday_forecast=True), db)
            check("flag on -> 'intraday' is traded", await on._selected_model_name() == "intraday")


async def test_trading_estimate_prefers_id_when_enabled():
    with tempfile.TemporaryDirectory() as d:
        async with Database(os.path.join(d, "s.sqlite3")) as db:
            fc = _client(StubIntraday(SHARP))
            off = _engine(Settings(trading_model="current"), db, fc)
            check("flag off -> trades the unsharpened forecast",
                  abs((await off._trading_estimate(STA, MKT, "2026-07-22", 86.0)).sigma_f - 4.5) < 1e-9)
            on = _engine(Settings(trading_model="current", enable_intraday_forecast=True), db, fc)
            check("flag on -> trades the sharpened forecast",
                  (await on._trading_estimate(STA, MKT, "2026-07-22", 86.0)).sigma_f <= 1.0)

            # A pinned non-'current' model goes through model_probabilities.
            pinned = _engine(Settings(trading_model="ensemble", enable_intraday_forecast=True), db, fc)
            est = await pinned._trading_estimate(STA, MKT, "2026-07-22", 86.0)
            check("pinned model with no ensemble available still returns an estimate",
                  est is not None)

            # No curve yet -> the _id variant is absent and we fall back cleanly.
            fc_none = _client(StubIntraday(None))
            fallback = _engine(Settings(trading_model="nbm", enable_intraday_forecast=True),
                               db, fc_none)
            est2 = await fallback._trading_estimate(STA, MKT, "2026-07-22", 86.0)
            check("missing _id variant falls back to an unsharpened estimate",
                  est2 is not None and abs(est2.sigma_f - 4.5) < 1e-9)


async def main():
    test_blend_precision_weighting(); test_blend_never_sharper_than_either_input()
    test_blend_sigma_floor_and_degenerate_inputs()
    await test_lookup_is_non_blocking_then_cached()
    await test_uncertainty_collapses_after_peak()
    await test_not_applicable_cases_return_none()
    await test_failed_build_backs_off_and_degrades()
    await test_wait_for_build_mode()
    await test_id_variants_emitted()
    await test_no_intraday_client_or_no_curve_changes_nothing()
    await test_intraday_failure_never_breaks_forecasting()
    await test_market_probability_fast_path_sharpening()
    test_base_model_name()
    await test_auto_selection_respects_the_flag()
    await test_pinned_intraday_model_is_gated_too()
    await test_trading_estimate_prefers_id_when_enabled()
    print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)

asyncio.run(main())
