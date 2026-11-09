"""Typed configuration with environment-variable loading.

Nothing in the system reads ``os.environ`` directly except :meth:`Settings.from_env`.
This keeps configuration explicit, testable, and easy to reason about.  Secrets
(Kalshi key id / private key path) are read from the environment; if they are
absent the system automatically drops into :attr:`Settings.dry_run` mode, in
which orders are fully validated, risk-checked and journaled to the ledger but
never transmitted to the exchange.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

from .models import LOCKIN_MARGIN_F, Venue, WeatherMarket


@dataclass(frozen=True, slots=True)
class StationConfig:
    """A monitored ASOS/METAR station.

    ``standard_utc_offset_hours`` is the *standard-time* (never DST) offset used
    to bucket observations into NWS climate days.
    """

    icao: str
    name: str
    latitude: float
    longitude: float
    standard_utc_offset_hours: int


# Stations Kalshi settles daily high-temperature series against (verified from
# each series' rules_primary text), plus KORD/KBOS. Offsets are Local Standard
# Time (no DST). ICAO -> Kalshi series mapping lives in Settings.weather_series.
DEFAULT_STATIONS: tuple[StationConfig, ...] = (
    StationConfig("KMDW", "Chicago Midway", 41.7860, -87.7524, -6),        # CST -> KXHIGHCHI
    StationConfig("KORD", "Chicago O'Hare", 41.9742, -87.9073, -6),        # CST (corroborates KMDW)
    StationConfig("KNYC", "New York Central Park", 40.7790, -73.9693, -5), # EST -> KXHIGHNY
    StationConfig("KMIA", "Miami International", 25.7932, -80.2906, -5),    # EST -> KXHIGHMIA
    StationConfig("KAUS", "Austin-Bergstrom", 30.1945, -97.6699, -6),      # CST -> KXHIGHAUS
    StationConfig("KDEN", "Denver International", 39.8617, -104.6731, -7),  # MST -> KXHIGHDEN
    StationConfig("KLAX", "Los Angeles International", 33.9416, -118.4085, -8),  # PST -> KXHIGHLAX
    StationConfig("KPHL", "Philadelphia International", 39.8729, -75.2437, -5),  # EST -> KXHIGHPHIL
    StationConfig("KBOS", "Boston Logan", 42.3606, -71.0097, -5),          # EST (observation only)
)


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Bankroll and execution risk parameters."""

    account_equity_usd: float = 10_000.0
    # Strategy 1 is deterministic (temperature already realized) so it warrants a
    # larger cap than the probabilistic Strategy 2.
    max_bankroll_fraction_per_market: float = 0.05     # 5% equity per ticker
    max_bankroll_fraction_model_edge: float = 0.02     # tighter cap for Strategy 2
    # Never pay above this for a "sure thing" YES in the lock-in strategy.
    lock_in_price_ceiling_cents: int = 98
    # Safety margin (deg F) the observed running max must clear beyond a bucket's
    # boundary before Strategy 1 calls the outcome deterministic.
    # 0.5 is EXACT mathematical certainty, not a fudge factor: Kalshi settles on
    # the ROUNDED whole-degF CLI high, so a bucket capped at 84 only becomes a
    # sure NO once the true max reaches 84.5. See models.deterministic_lock_in.
    # ⚠ Lowering this re-opens a measured defect: at the old effective 0.0 the
    # rule made 18 wrong calls in 564 replayed detections (scripts/replay_lockins.py).
    lockin_margin_f: float = LOCKIN_MARGIN_F
    # Minimum modeled EV edge (probability points) to act on Strategy 2.
    model_edge_threshold: float = 0.15
    # --- Time-decay edge threshold (backlog 0b; opt-in via enable_dynamic_edge) ---
    # Early in the climate day the forecast carries the whole answer and a model
    # "edge" is mostly noise; late in the day the running max has largely decided
    # the outcome, so a smaller edge is more trustworthy. These interpolate the
    # threshold linearly in LOCAL STANDARD TIME between the two hours below,
    # flat outside them. Hours are LST per station (cities span UTC-5..-8).
    # ⚠ UNMEASURED defaults. The pre-2026-08-23 threshold sweep is void (it was
    # fitted against probabilities that summed to 0.70). Run
    # `backtest --strategy 2 --sweep-dynamic` before trusting any of these.
    dynamic_edge_max_threshold: float = 0.25   # at/before start_hour
    dynamic_edge_min_threshold: float = 0.05   # at/after end_hour
    dynamic_edge_start_hour: float = 8.0
    dynamic_edge_end_hour: float = 16.0
    # Signal persistence: a qualifying edge must survive this long, on the SAME
    # side, before it is acted on. Deliberately not "N evaluation loops":
    # arb_eval_seconds is 120 s while the gridpoint forecast is cached for
    # forecast.refresh_seconds (900 s), so consecutive loops mostly re-read one
    # cached forecast and would gate on order-book jitter, not model stability.
    edge_persist_seconds: float = 900.0
    max_contracts_per_order: int = 5_000
    # Fractional-Kelly sizing for model-edge orders (0 = off -> flat per-market
    # cap only, today's behavior). e.g. 0.25 = quarter-Kelly on the modeled edge.
    kelly_fraction: float = 0.0
    # Correlation cap: max equity fraction across a correlated group (all buckets
    # of one station/day are the same underlying). 0 = off. Recommended ~0.10 live.
    max_bankroll_fraction_per_group: float = 0.0


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    """Strategy-1 discovery experiment: observation only, never gates a trade.

    Everything here writes to `lockin_events` / `book_snapshots` /
    `sweep_metrics` / `source_observations` and is read by
    `python -m weather_arb.experiment_report`. Turning it off costs measurement,
    never safety.
    """

    enabled: bool = True

    # --- order-book sampling ------------------------------------------------
    # Control cadence for EVERY tracked market. This is the null hypothesis: to
    # tell an event-driven discount from ordinary microstructure you need the
    # book when nothing is happening, which the system has never recorded.
    control_interval_seconds: float = 600.0
    # Faster cadence once a market's running max is within `arm_threshold_f` of
    # its certainty bound, so the minutes BEFORE a detection are captured -- the
    # t-10/-5/-2/-1 offsets cannot be sampled retroactively.
    armed_interval_seconds: float = 60.0
    arm_threshold_f: float = 3.0
    # Follow-ups after a detection, for the repricing decay curve.
    offsets_seconds: tuple[float, ...] = (60.0, 120.0, 300.0, 600.0, 1800.0)
    tick_seconds: float = 30.0
    # Hard ceiling on order-book GETs per tick. Kalshi's client-side bucket is
    # 8/s and the engine already spends it on evaluation; this keeps the
    # recorder a junior partner rather than a competitor for the same budget.
    max_captures_per_tick: int = 12

    # --- secondary observation source (api.weather.gov 5-minute product) ----
    # FREE, no key, and it leads the hourly METAR by a measured median 53 min at
    # 5 of 7 stations. Shadow only: its whole-degC quantization (1.8 degF) is
    # COARSER than the +-0.08 degF METAR error that caused the lock-in defect,
    # so it must be proven not to manufacture false crossings before it could
    # ever be trusted to prove one. See ArbitrageEngine.observe_shadow_lockin.
    enable_nws_5min: bool = True
    nws_5min_poll_seconds: float = 120.0
    nws_5min_url: str = "https://api.weather.gov/stations/{station}/observations"
    nws_5min_limit: int = 24


@dataclass(frozen=True, slots=True)
class ForecastConfig:
    """Parameters for the forecast probability model."""

    # Base standard deviation (deg F) of the daily-max forecast error.  Real NBM
    # error grows with lead time; we scale this base by a lead-time factor.
    base_sigma_f: float = 2.5
    max_sigma_f: float = 7.0
    sigma_per_hour_lead_f: float = 0.06
    refresh_seconds: float = 900.0    # re-pull gridded forecasts every 15 min
    # Climatology base-rate model (IEM archive): windowed multi-year history.
    # Also sizes the intraday remaining-rise curve (same archive, same window).
    climo_years: int = 12
    climo_window_days: int = 7
    climo_min_samples: int = 60
    # Floor on the intraday model's sigma (deg F). It is a settlement-BASIS
    # allowance, not weather uncertainty: our running max comes from hourly
    # METARs, while Kalshi settles on the NWS CLI daily high, derived from the
    # continuous ASOS record, which is never lower and usually higher.
    #
    # DERIVED, not guessed (`python -m weather_arb.settlement_basis`, 245 settled
    # events, 7 stations, 2026-07-22..2026-08-23):
    #   * The CLI high reproduces Kalshi's settled bucket on 245/245 events
    #     (100%, every city). The hourly-METAR max only manages 62%.
    #   * R = settlement - hourly-METAR max: mean +0.81, sd 0.71, RMS 1.08,
    #     range [0, 3], and NEVER negative in 245 samples.
    #   * From ~LST 16:00 the intraday remaining-rise curve is 0.00 +- 0.00, so
    #     late in the day the model is exactly N(running_max, floor) truncated
    #     below at running_max -- a half-normal with scale = this floor. The MLE
    #     of a half-normal scale is RMS(R) = 1.08.
    # Held at 1.0 because 1.0 and 1.08 are statistically indistinguishable out of
    # sample (test NLL splits 2-2 across four walk-forward folds), and moving a
    # live constant on a tie would perturb the in-flight `_id` Brier experiment
    # for no measured gain. Re-run the command before changing it; it also prints
    # the per-station RMS (DEN is the outlier at 1.52).
    # Do NOT set this to 0: the basis gap it covers is real and measured.
    intraday_sigma_floor_f: float = 1.0


@dataclass(frozen=True, slots=True)
class KalshiConfig:
    """Kalshi REST + WebSocket endpoints and credentials."""

    rest_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    ws_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    api_key_id: Optional[str] = None
    private_key_path: Optional[str] = None
    # Token-bucket rate limiting for REST calls.
    rate_limit_per_second: float = 8.0
    rate_limit_burst: int = 16
    request_timeout_seconds: float = 15.0

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key_id and self.private_key_path)


@dataclass(frozen=True, slots=True)
class Settings:
    """Root configuration object for the whole process."""

    db_path: str = "weather_arb.sqlite3"
    log_level: str = "INFO"
    dry_run: bool = True

    stations: tuple[StationConfig, ...] = DEFAULT_STATIONS
    markets: tuple[WeatherMarket, ...] = ()

    metar_poll_seconds: float = 45.0
    metar_source: str = "aviationweather"     # or "iem"
    # Periodic re-evaluation cadence. With many markets this drives most REST
    # traffic, so keep it modest -- deterministic lock-in latency does NOT depend
    # on it (the METAR poller fires on_new_high immediately on a validated max);
    # this loop mainly refreshes Strategy-2 (forecast edge), which moves slowly.
    arb_eval_seconds: float = 120.0
    orderbook_refresh_seconds: float = 15.0

    # Trading forecast model: "auto" picks the lowest-Brier model once enough
    # settled samples exist (else falls back to "current"); or pin a name
    # (current / nbm / gfs_mos / ensemble / *_bc).
    trading_model: str = "auto"
    model_select_min_samples: int = 20

    # Append a shadow-prediction snapshot to model_prediction_history at most this
    # often per (ticker, model), bounding growth while giving the Strategy-2
    # backtest a lead-time series. 0 = snapshot every evaluation.
    prediction_history_min_interval_seconds: float = 1800.0

    # Cross-venue basis monitor scope: "us" (Kalshi / ForecastEx / Polymarket-US)
    # or "global" (adds UK/EU exchanges Betfair / Smarkets). Reference-only --
    # it never affects the Kalshi trading path. See :mod:`cross_venue`.
    venue_scope: str = "us"
    # Estimated per-contract, per-side fee (whole cents) on IBKR ForecastEx.
    # Used only to net cross-venue lock math; confirm against your commission tier.
    forecastex_fee_cents_per_contract: int = 1
    # Base URL of an authenticated IBKR Client Portal Web API session (e.g.
    # "https://localhost:5000/v1/api"). ForecastEx has no public quote feed, so
    # the cross-venue live monitor can only pull its quotes when this is set;
    # otherwise it runs Polymarket-only. Reference/monitoring only, never trades.
    ibkr_web_api_base: Optional[str] = None
    # The local gateway serves a self-signed cert, so TLS verify is off by
    # default; the client only ever talks to ibkr_web_api_base.
    ibkr_verify_ssl: bool = False
    # City -> ForecastEx underlying symbol, for auto-discovering daily-high
    # strike contracts via the CPAPI secdef endpoints (WA_FORECASTEX_SYMBOLS_JSON).
    forecastex_symbols: tuple[tuple[str, str], ...] = ()
    # Explicit city -> [(strike_f, conid)] override (WA_FORECASTEX_CONTRACTS_JSON).
    # Use when auto-discovery needs help: paste today's ForecastEx threshold conids
    # for a city and the monitor snapshots them directly. Bypasses discovery.
    forecastex_contracts: tuple[tuple[str, float, int], ...] = ()

    def forecastex_symbol_for(self, city: str) -> Optional[str]:
        for c, sym in self.forecastex_symbols:
            if c == city:
                return sym
        return None

    def forecastex_contracts_for(self, city: str) -> tuple[tuple[float, int], ...]:
        return tuple((s, cid) for c, s, cid in self.forecastex_contracts if c == city)

    # 1-minute ASOS lock-in augmenter (opt-in, off by default so enabling it is a
    # deliberate change). Kalshi settles on the NWS CLI, derived from the full
    # 1-minute record; this feeds intra-hour spike highs the routine METAR poller
    # misses into the same T_max/on_new_high pipeline (only ever RAISING T_max
    # toward the true NWS max). Safety: candidate max must be corroborated by
    # >= onemin_min_confirm minutes and must not exceed T_max by more than
    # spike_threshold_f. The IEM 1-min archive lags ~1h, so this adds lock
    # accuracy/completeness, not latency (METAR stays the low-latency path).
    enable_1min_lockin: bool = False
    onemin_poll_seconds: float = 300.0
    onemin_min_confirm: int = 2

    # Passive model-edge placement (opt-in, off by default). Strategy 2 is a taker
    # today: it only trades when the ask is already at/below the fee-adjusted fair
    # price. With this on, when it CAN'T take profitably it instead rests a bid
    # below the ask (or an ask above the bid), improving the touch by
    # passive_improve_ticks but never crossing and never worse than the edge limit,
    # to capture the spread on model-edge (no-rush) trades. May not fill.
    enable_passive_model_edge: bool = False
    passive_improve_ticks: int = 1
    # Backlog 0b, deliberately TWO flags rather than one: measured 2026-08-23, the
    # two halves point in OPPOSITE directions, so coupling them would force you to
    # take the harmful half to get the useful one.
    #   time-decay threshold -> measured HARMFUL on corrected probabilities
    #                           (every config lost to the flat 0.15). Keep off.
    #   persistence gate     -> mildly positive in both windows, unproven. Off.
    enable_dynamic_edge: bool = False        # time-decay threshold only
    enable_edge_persistence: bool = False    # signal-persistence gate only
    # Per-strategy LIVE clearance. dry_run is global, so without this, flipping
    # WA_DRY_RUN=false to trade Strategy 1 (near-riskless lock-in) would also put
    # Strategy 2 live -- and Strategy 2 measures -$3.26/trade. This blocks
    # model-edge orders when, and only when, real money is at stake: dry-run
    # behaviour is untouched, so paper measurement of Strategy 2 continues.
    allow_live_model_edge: bool = False

    # Empirical probability recalibration for Strategy 2 (opt-in, off by default).
    # Diagnosed 2026-08-14: raw model probabilities are well-calibrated in aggregate
    # (Brier 0.14-0.19) but overconfident specifically on the cheap/longshot buckets
    # the edge-threshold rule disproportionately selects (2% realized hit rate on
    # <=20c entries in a 21-day backtest). See weather_arb.calibration and
    # `backtest --strategy 2 --calibrate` for the out-of-sample validation this
    # should clear before being turned on live. Loads the model_calibration cache
    # written by `backtest --strategy 2 --calibrate --persist`.
    enable_probability_calibration: bool = False
    # Refuse a model-edge entry priced below this many cents regardless of edge --
    # cheap defense-in-depth against the same overconfidence failure mode. 0 = off.
    model_edge_min_price_cents: int = 0

    # Intraday sharpening of the live Strategy-2 forecast (opt-in, off by default).
    # The static forecast scales sigma by lead time alone (~4.5 deg F live), which
    # smears probability into the cheap longshot buckets that lose money. The
    # intraday remaining-rise climatology (:mod:`weather_arb.intraday`) collapses
    # that uncertainty as the day progresses; blending it in attacks the diagnosed
    # root cause rather than the symptom. The blended ``<model>_id`` variants are
    # ALWAYS logged as shadow predictions (free measurement, graded by Brier and by
    # `backtest --strategy 2` like every other model); this flag is what makes
    # Strategy 2 actually trade them.
    enable_intraday_forecast: bool = False

    # Auto-discovery of Kalshi weather markets.
    enable_discovery: bool = True
    discovery_refresh_seconds: float = 1800.0     # re-scan markets every 30 min
    settlement_poll_seconds: float = 300.0        # check for settlements every 5 min
    # Max markets graded for settlement per reconciler pass. Grading covers every
    # SHADOW-PREDICTED market (not just filled ones) so the Brier leaderboard and
    # WA_TRADING_MODEL=auto have outcomes to work with; this bounds the REST calls
    # a backlog can generate in one pass.
    settlement_grade_batch: int = 200
    # Mapping of Kalshi high-temp series ticker -> the ICAO station Kalshi
    # settles it against. THESE MUST MATCH KALSHI'S OFFICIAL SETTLEMENT SOURCE;
    # verify against each series' rules before trading. Only series whose station
    # is also in ``stations`` are traded (so observed Tmax matches settlement).
    weather_series: tuple[tuple[str, str], ...] = (
        ("KXHIGHNY", "KNYC"),     # NYC -> Central Park
        ("KXHIGHCHI", "KMDW"),    # Chicago -> Midway
        ("KXHIGHMIA", "KMIA"),    # Miami -> Miami International
        ("KXHIGHAUS", "KAUS"),    # Austin -> Austin-Bergstrom
        ("KXHIGHDEN", "KDEN"),    # Denver -> Denver International
        ("KXHIGHLAX", "KLAX"),    # Los Angeles -> LAX
        ("KXHIGHPHIL", "KPHL"),   # Philadelphia -> Philadelphia International
    )

    # Glitch protection (see metar_ingest).
    # spike_threshold_f is now only the FALLBACK for a reading with no velocity
    # reference (first observation after a restart) and the 1-minute augmenter's
    # sanity cap; the live guard is kinematic (below).
    spike_threshold_f: float = 5.0            # jump size that requires confirmation
    confirmation_window_seconds: float = 300.0  # 5 minutes
    confirmation_tolerance_f: float = 1.5

    # Kinematic (velocity) guard on a RISING temperature -- see metar_ingest.
    # A flat spike_threshold_f rejected ordinary morning warm-up wherever the air
    # routinely climbs faster than it: METARs arrive hourly, so a >5 F/hour rise
    # could never meet the 5-minute confirmation window and validated_max_f froze
    # near the morning value (measured 2026-08-21: daily_tmax short by >=2 F on 21%
    # of city-days, mean -11.5 F at DEN and -7.2 F at AUS, while MIA/LAX were
    # perfect). The replacement bounds the RATE of warming instead of the step, so
    # a fast-warming station is no longer penalised for reporting hourly.
    max_warming_rate_f_per_hour: float = 8.0   # believable sustained warming rate
    warming_rate_buffer_f: float = 1.0         # rounding/jitter slack on top
    max_absolute_jump_f: float = 15.0          # hard cap, no matter how long the gap
    rapid_update_seconds: float = 60.0         # "same minute" updates (SPECI bursts)
    rapid_update_max_jump_f: float = 2.0       # allowance for those

    cancel_orders_on_shutdown: bool = True
    http_user_agent: str = "weather-arb/1.0 (contact: ops@example.com)"

    # Estimated Kalshi trading-fee coefficient (fee = coeff * C * P * (1-P),
    # rounded up to the cent). Used to net fees out of paper P&L and to make the
    # Strategy-2 edge test fee-aware.
    kalshi_fee_coefficient: float = 0.07
    heartbeat_seconds: float = 600.0     # periodic status summary log

    # Alerting / watchdog.
    ntfy_url: Optional[str] = None       # e.g. https://ntfy.sh/<topic>
    heartbeat_stale_minutes: float = 40.0
    # A fresh heartbeat only proves the supervisor loop is turning. This is how
    # long the tracker may go without producing a signal, detection or
    # observation before the watchdog treats it as broken-but-breathing.
    # Generous by default: METAR is hourly and quiet stretches are normal.
    watchdog_activity_minutes: float = 180.0
    tracker_log_path: str = "logs/tracker.out.log"

    risk: RiskConfig = field(default_factory=RiskConfig)
    forecast: ForecastConfig = field(default_factory=ForecastConfig)
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    experiment: ExperimentConfig = field(default_factory=ExperimentConfig)

    def station_by_icao(self, icao: str) -> Optional[StationConfig]:
        for station in self.stations:
            if station.icao == icao:
                return station
        return None

    def markets_for_station(self, icao: str) -> tuple[WeatherMarket, ...]:
        return tuple(m for m in self.markets if m.icao == icao)

    # ------------------------------------------------------------------ #
    # Environment loading
    # ------------------------------------------------------------------ #
    @classmethod
    def from_env(cls) -> "Settings":
        """Build a :class:`Settings` from environment variables with defaults.

        Recognized variables (all optional):

        ``WA_DB_PATH``, ``WA_LOG_LEVEL``, ``WA_DRY_RUN``,
        ``WA_ACCOUNT_EQUITY_USD``, ``WA_METAR_POLL_SECONDS``,
        ``WA_ARB_EVAL_SECONDS``, ``KALSHI_API_KEY_ID``,
        ``KALSHI_PRIVATE_KEY_PATH``, ``KALSHI_REST_BASE_URL``,
        ``KALSHI_WS_URL``, ``WA_MARKETS_JSON`` (see below).

        ``WA_MARKETS_JSON`` is a JSON array of objects::

            [{"ticker": "...", "icao": "KORD", "strike_temp_f": 90,
              "comparison": ">=", "title": "..."}]
        """
        # A default INSTANCE, never RiskConfig.field: slots=True makes the class
        # attribute a slot descriptor, not the default value.
        risk_defaults = RiskConfig()
        risk = RiskConfig(
            account_equity_usd=_env_float("WA_ACCOUNT_EQUITY_USD", 10_000.0),
            max_bankroll_fraction_per_market=_env_float("WA_MAX_FRACTION_PER_MARKET", 0.05),
            max_bankroll_fraction_model_edge=_env_float("WA_MAX_FRACTION_MODEL_EDGE", 0.02),
            model_edge_threshold=_env_float("WA_MODEL_EDGE_THRESHOLD", 0.15),
            dynamic_edge_max_threshold=_env_float(
                "WA_DYNAMIC_EDGE_MAX_THRESHOLD", risk_defaults.dynamic_edge_max_threshold),
            dynamic_edge_min_threshold=_env_float(
                "WA_DYNAMIC_EDGE_MIN_THRESHOLD", risk_defaults.dynamic_edge_min_threshold),
            dynamic_edge_start_hour=_env_float(
                "WA_DYNAMIC_EDGE_START_HOUR", risk_defaults.dynamic_edge_start_hour),
            dynamic_edge_end_hour=_env_float(
                "WA_DYNAMIC_EDGE_END_HOUR", risk_defaults.dynamic_edge_end_hour),
            edge_persist_seconds=_env_float(
                "WA_EDGE_PERSIST_SECONDS", risk_defaults.edge_persist_seconds),
            kelly_fraction=_env_float("WA_KELLY_FRACTION", 0.0),
            max_bankroll_fraction_per_group=_env_float("WA_MAX_FRACTION_PER_GROUP", 0.0),
            lockin_margin_f=_env_float(
                "WA_LOCKIN_MARGIN_F", risk_defaults.lockin_margin_f),
        )

        # NB: read defaults from a default *instance*, not the class -- on a
        # slots=True dataclass, ``KalshiConfig.rest_base_url`` is the slot
        # descriptor, not the field's default value.
        experiment_defaults = ExperimentConfig()
        experiment = ExperimentConfig(
            enabled=_as_bool(os.environ.get(
                "WA_ENABLE_EXPERIMENT",
                "true" if experiment_defaults.enabled else "false")),
            control_interval_seconds=_env_float(
                "WA_EXPERIMENT_CONTROL_SECONDS",
                experiment_defaults.control_interval_seconds),
            armed_interval_seconds=_env_float(
                "WA_EXPERIMENT_ARMED_SECONDS",
                experiment_defaults.armed_interval_seconds),
            arm_threshold_f=_env_float(
                "WA_EXPERIMENT_ARM_THRESHOLD_F",
                experiment_defaults.arm_threshold_f),
            tick_seconds=_env_float(
                "WA_EXPERIMENT_TICK_SECONDS", experiment_defaults.tick_seconds),
            max_captures_per_tick=_env_int(
                "WA_EXPERIMENT_MAX_CAPTURES", experiment_defaults.max_captures_per_tick),
            enable_nws_5min=_as_bool(os.environ.get(
                "WA_ENABLE_NWS_5MIN",
                "true" if experiment_defaults.enable_nws_5min else "false")),
            nws_5min_poll_seconds=_env_float(
                "WA_NWS_5MIN_POLL_SECONDS",
                experiment_defaults.nws_5min_poll_seconds),
        )

        kalshi_defaults = KalshiConfig()
        kalshi = KalshiConfig(
            rest_base_url=os.environ.get(
                "KALSHI_REST_BASE_URL", kalshi_defaults.rest_base_url
            ),
            ws_url=os.environ.get("KALSHI_WS_URL", kalshi_defaults.ws_url),
            api_key_id=os.environ.get("KALSHI_API_KEY_ID"),
            private_key_path=os.environ.get("KALSHI_PRIVATE_KEY_PATH"),
        )

        forecast_defaults = ForecastConfig()
        forecast = ForecastConfig(
            intraday_sigma_floor_f=_env_float(
                "WA_INTRADAY_SIGMA_FLOOR_F", forecast_defaults.intraday_sigma_floor_f
            ),
        )

        markets = _parse_markets_env(os.environ.get("WA_MARKETS_JSON"))

        # Default to dry-run whenever credentials are missing, so the process is
        # always safe to boot.
        explicit_dry_run = os.environ.get("WA_DRY_RUN")
        if explicit_dry_run is not None:
            dry_run = _as_bool(explicit_dry_run)
        else:
            dry_run = not kalshi.has_credentials

        weather_series = _parse_series_env(os.environ.get("WA_WEATHER_SERIES_JSON"))
        enable_discovery_raw = os.environ.get("WA_ENABLE_DISCOVERY")
        enable_discovery = (
            _as_bool(enable_discovery_raw) if enable_discovery_raw is not None else True
        )

        kwargs: dict[str, object] = dict(
            db_path=os.environ.get("WA_DB_PATH", "weather_arb.sqlite3"),
            log_level=os.environ.get("WA_LOG_LEVEL", "INFO"),
            dry_run=dry_run,
            markets=markets,
            metar_poll_seconds=_env_float("WA_METAR_POLL_SECONDS", 45.0),
            metar_source=os.environ.get("WA_METAR_SOURCE", "aviationweather"),
            arb_eval_seconds=_env_float("WA_ARB_EVAL_SECONDS", 120.0),
            enable_discovery=enable_discovery,
            trading_model=os.environ.get("WA_TRADING_MODEL", "auto"),
            model_select_min_samples=int(_env_float("WA_MODEL_SELECT_MIN_SAMPLES", 20)),
            prediction_history_min_interval_seconds=_env_float(
                "WA_PREDICTION_HISTORY_INTERVAL_SECONDS", 1800.0),
            enable_1min_lockin=_as_bool(os.environ.get("WA_ENABLE_1MIN_LOCKIN", "false")),
            onemin_poll_seconds=_env_float("WA_ONEMIN_POLL_SECONDS", 300.0),
            onemin_min_confirm=int(_env_float("WA_ONEMIN_MIN_CONFIRM", 2)),
            max_warming_rate_f_per_hour=_env_float("WA_MAX_WARMING_RATE_F_PER_HOUR", 8.0),
            warming_rate_buffer_f=_env_float("WA_WARMING_RATE_BUFFER_F", 1.0),
            max_absolute_jump_f=_env_float("WA_MAX_ABSOLUTE_JUMP_F", 15.0),
            rapid_update_seconds=_env_float("WA_RAPID_UPDATE_SECONDS", 60.0),
            rapid_update_max_jump_f=_env_float("WA_RAPID_UPDATE_MAX_JUMP_F", 2.0),
            enable_dynamic_edge=_as_bool(
                os.environ.get("WA_ENABLE_DYNAMIC_EDGE", "false")),
            enable_edge_persistence=_as_bool(
                os.environ.get("WA_ENABLE_EDGE_PERSISTENCE", "false")),
            allow_live_model_edge=_as_bool(
                os.environ.get("WA_ALLOW_LIVE_MODEL_EDGE", "false")),
            enable_passive_model_edge=_as_bool(
                os.environ.get("WA_ENABLE_PASSIVE_MODEL_EDGE", "false")),
            passive_improve_ticks=int(_env_float("WA_PASSIVE_IMPROVE_TICKS", 1)),
            enable_probability_calibration=_as_bool(
                os.environ.get("WA_ENABLE_PROBABILITY_CALIBRATION", "false")),
            model_edge_min_price_cents=int(_env_float("WA_MODEL_EDGE_MIN_PRICE_CENTS", 0)),
            enable_intraday_forecast=_as_bool(
                os.environ.get("WA_ENABLE_INTRADAY_FORECAST", "false")),
            venue_scope=os.environ.get("WA_VENUE_SCOPE", "us").strip().lower(),
            ibkr_web_api_base=os.environ.get("IBKR_WEB_API_BASE") or None,
            ibkr_verify_ssl=_as_bool(os.environ.get("IBKR_VERIFY_SSL", "false")),
            forecastex_symbols=_parse_fx_symbols(os.environ.get("WA_FORECASTEX_SYMBOLS_JSON")),
            forecastex_contracts=_parse_fx_contracts(os.environ.get("WA_FORECASTEX_CONTRACTS_JSON")),
            kalshi_fee_coefficient=_env_float("WA_KALSHI_FEE_COEFF", 0.07),
            heartbeat_seconds=_env_float("WA_HEARTBEAT_SECONDS", 600.0),
            ntfy_url=_ntfy_url_from_env(),
            heartbeat_stale_minutes=_env_float("WA_HEARTBEAT_STALE_MINUTES", 40.0),
            watchdog_activity_minutes=_env_float("WA_WATCHDOG_ACTIVITY_MINUTES", 180.0),
            risk=risk,
            forecast=forecast,
            kalshi=kalshi,
            experiment=experiment,
        )
        if weather_series is not None:
            kwargs["weather_series"] = weather_series
        return cls(**kwargs)  # type: ignore[arg-type]


def _parse_markets_env(raw: Optional[str]) -> tuple[WeatherMarket, ...]:
    if not raw:
        return ()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("WA_MARKETS_JSON must be a JSON array")
    markets: list[WeatherMarket] = []
    for entry in data:
        markets.append(
            WeatherMarket(
                ticker=str(entry["ticker"]),
                venue=Venue(str(entry.get("venue", "kalshi"))),
                icao=str(entry["icao"]),
                strike_temp_f=float(entry["strike_temp_f"]),
                comparison=str(entry.get("comparison", ">=")),
                title=str(entry.get("title", "")),
            )
        )
    return tuple(markets)


def _parse_series_env(raw: Optional[str]) -> Optional[tuple[tuple[str, str], ...]]:
    """Parse ``WA_WEATHER_SERIES_JSON`` -> tuple of (series_ticker, icao) pairs.

    Accepts a JSON object ``{"KXHIGHNY": "KNYC", ...}`` or an array of pairs
    ``[["KXHIGHNY", "KNYC"], ...]``.
    """
    if not raw:
        return None
    data = json.loads(raw)
    if isinstance(data, dict):
        return tuple((str(k), str(v)) for k, v in data.items())
    if isinstance(data, list):
        return tuple((str(pair[0]), str(pair[1])) for pair in data)
    raise ValueError("WA_WEATHER_SERIES_JSON must be a JSON object or array of pairs")


def _parse_fx_symbols(raw: Optional[str]) -> tuple[tuple[str, str], ...]:
    """WA_FORECASTEX_SYMBOLS_JSON -> ((city, symbol), ...).

    Accepts ``{"LAX": "SYM", ...}`` or ``[["LAX","SYM"], ...]``.
    """
    if not raw:
        return ()
    data = json.loads(raw)
    if isinstance(data, dict):
        return tuple((str(k).upper(), str(v)) for k, v in data.items())
    if isinstance(data, list):
        return tuple((str(p[0]).upper(), str(p[1])) for p in data)
    raise ValueError("WA_FORECASTEX_SYMBOLS_JSON must be an object or array of pairs")


def _parse_fx_contracts(raw: Optional[str]) -> tuple[tuple[str, float, int], ...]:
    """WA_FORECASTEX_CONTRACTS_JSON -> ((city, strike_f, conid), ...).

    Accepts ``{"LAX": [[76, 123456], [78, 123457]], ...}`` (strike, conid pairs).
    """
    if not raw:
        return ()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("WA_FORECASTEX_CONTRACTS_JSON must be a {city: [[strike, conid], ...]} object")
    out: list[tuple[str, float, int]] = []
    for city, rows in data.items():
        for strike, conid in rows:
            out.append((str(city).upper(), float(strike), int(conid)))
    return tuple(out)


def _ntfy_url_from_env() -> Optional[str]:
    url = os.environ.get("WA_NTFY_URL")
    if url:
        return url
    topic = os.environ.get("WA_NTFY_TOPIC")
    return f"https://ntfy.sh/{topic}" if topic else None


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}
