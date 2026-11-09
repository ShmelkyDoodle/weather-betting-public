"""Intraday model: remaining-rise samples, curve build, prediction (offline)."""
import asyncio, sys
from datetime import datetime, timezone
import os as _osp; sys.path.insert(0, _osp.path.dirname(_osp.path.dirname(_osp.path.abspath(__file__))))
from weather_arb.intraday import remaining_rise_samples, build_curve, predict

failures = []
def check(n, c):
    print(("PASS" if c else "FAIL"), "-", n)
    if not c: failures.append(n)

OFF = -6  # CST


def _ts(dd, hh):
    return datetime(2026, 7, dd, hh, tzinfo=timezone.utc)


def test_samples_and_collapse():
    # Two LST days, obs at 12:00 & 14:00 LST (18:00Z & 20:00Z at offset -6).
    obs = [
        (_ts(20, 18), 80.0), (_ts(20, 20), 90.0),   # day A: max 90 -> rise 12h=10, 14h=0
        (_ts(21, 18), 82.0), (_ts(21, 20), 88.0),   # day B: max 88 -> rise 12h=6,  14h=0
    ]
    s = remaining_rise_samples(obs, OFF)
    check("hour 12 samples = [10, 6]", sorted(s[12]) == [6.0, 10.0])
    check("hour 14 samples = [0, 0] (post-peak collapse)", s[14] == [0.0, 0.0])


def test_build_and_predict():
    obs = [
        (_ts(20, 18), 80.0), (_ts(20, 20), 90.0),
        (_ts(21, 18), 82.0), (_ts(21, 20), 88.0),
    ]
    curve = build_curve(remaining_rise_samples(obs, OFF), min_samples=2)
    check("hour 12 mean rise = 8.0", abs(curve[12][0] - 8.0) < 1e-9)
    check("hour 14 mean rise = 0.0 (max locked)", curve[14][0] == 0.0)
    # Pre-peak: predicted max = running + mean rise, sigma floored.
    p12 = predict(curve, 12, running_max_now=85.0)
    check("noon prediction 85 + 8 = 93", p12 is not None and abs(p12.predicted_max_f - 93.0) < 1e-9)
    check("sigma floored to >= 1", p12.sigma_f >= 1.0)
    # Post-peak: rise 0 -> daily max == running max (already in).
    p14 = predict(curve, 14, running_max_now=91.0)
    check("post-peak prediction == running max", p14 is not None and p14.predicted_max_f == 91.0)
    # Missing hour -> None.
    check("missing hour -> None", predict(curve, 3, 70.0) is None)


def test_min_samples_filter():
    obs = [(_ts(20, 18), 80.0), (_ts(20, 20), 90.0)]  # only 1 day -> 1 sample/hour
    curve = build_curve(remaining_rise_samples(obs, OFF), min_samples=2)
    check("single-sample hours dropped below min_samples", curve == {})


async def main():
    test_samples_and_collapse(); test_build_and_predict(); test_min_samples_filter()
    print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    sys.exit(1 if failures else 0)

asyncio.run(main())
