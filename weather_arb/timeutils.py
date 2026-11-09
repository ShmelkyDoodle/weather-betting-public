"""UTC-first time utilities and NWS climate-day (LST) helpers.

Every timestamp that flows through the system is stored and reasoned about in
**UTC**.  The one place local time matters is deciding which *climate day* an
observation belongs to.  US National Weather Service daily climate products
(the daily maximum/minimum temperature that weather contracts settle against)
are bucketed by **Local Standard Time (LST)** -- i.e. the fixed standard-time
offset for the station, *ignoring Daylight Saving Time*.

Using civil (DST-aware) local time here is a classic and expensive bug: during
summer it would shift the midnight-to-midnight boundary by an hour and could
mis-attribute a late-evening or early-morning reading to the wrong day, causing
the running T_max used for lock-in arbitrage to be computed against the wrong
settlement window.  We therefore convert with a *fixed* offset only.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC :class:`datetime`."""
    return datetime.now(timezone.utc)


def to_utc(dt: datetime) -> datetime:
    """Coerce any :class:`datetime` to timezone-aware UTC.

    Naive datetimes are *assumed* to already be UTC (the system never produces
    naive local timestamps), aware datetimes are converted.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def from_epoch_seconds(epoch_seconds: float) -> datetime:
    """Convert a Unix epoch (seconds) into an aware UTC datetime."""
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)


def isoformat_utc(dt: datetime) -> str:
    """Serialize a datetime to a canonical ``...+00:00`` UTC ISO-8601 string."""
    return to_utc(dt).isoformat()


def parse_isoformat_utc(value: str) -> datetime:
    """Inverse of :func:`isoformat_utc`; tolerates a trailing ``Z``."""
    normalized = value.replace("Z", "+00:00")
    return to_utc(datetime.fromisoformat(normalized))


def climate_date_for(dt_utc: datetime, standard_utc_offset_hours: int) -> date:
    """Return the NWS climate day (Local Standard Time) for ``dt_utc``.

    Parameters
    ----------
    dt_utc:
        Any datetime; it is normalized to UTC first.
    standard_utc_offset_hours:
        The station's *standard-time* offset from UTC (e.g. ``-6`` for CST,
        ``-5`` for EST).  DST is deliberately not applied.

    Returns
    -------
    datetime.date
        The local-standard-time calendar date, i.e. the settlement window the
        observation contributes its high/low temperature to.
    """
    local_standard = to_utc(dt_utc) + timedelta(hours=standard_utc_offset_hours)
    return local_standard.date()


def local_hour_lst(dt_utc: datetime, standard_utc_offset_hours: int) -> float:
    """Fractional hour-of-day in Local Standard Time, e.g. ``14.5`` for 14:30 LST.

    Same fixed-offset, no-DST convention as :func:`climate_date_for` -- the one
    Kalshi settles on. Fractional (not integer) so a time-of-day schedule decays
    smoothly instead of stepping once an hour.
    """
    local = to_utc(dt_utc) + timedelta(hours=standard_utc_offset_hours)
    return local.hour + local.minute / 60.0 + local.second / 3600.0


def climate_date_str(dt_utc: datetime, standard_utc_offset_hours: int) -> str:
    """String form of :func:`climate_date_for` (``YYYY-MM-DD``)."""
    return climate_date_for(dt_utc, standard_utc_offset_hours).isoformat()


def climate_day_bounds_utc(
    climate_date: str, standard_utc_offset_hours: int
) -> tuple[datetime, datetime]:
    """UTC ``[start, end)`` covering the LST climate day ``YYYY-MM-DD``."""
    year, month, day = (int(part) for part in climate_date.split("-"))
    local_midnight_utc = datetime(
        year, month, day, tzinfo=timezone.utc
    ) - timedelta(hours=standard_utc_offset_hours)
    return local_midnight_utc, local_midnight_utc + timedelta(days=1)
