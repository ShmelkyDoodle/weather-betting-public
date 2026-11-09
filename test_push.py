#!/usr/bin/env python
"""Fire one test push through the real alert path and PROVE it was accepted.

    .venv/bin/python test_push.py

Why this is more than five lines: `AlertManager.send` catches every exception and
returns None, so calling it tells you nothing about whether the push actually
left the building. This sends through the real AlertManager (the same code path a
Strategy-1 lock-in uses) and then independently reads the topic back from ntfy to
confirm the server has the message -- separating "we failed to send" from "it sent
fine and the phone isn't subscribed".
"""
import json
import os
import pathlib
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# --- the five lines that matter ------------------------------------------- #
_ENV = pathlib.Path(__file__).with_name(".env")
if not _ENV.exists():
    raise SystemExit("no .env -- copy .env.example to .env and set WA_NTFY_TOPIC")
for _line in _ENV.read_text().splitlines():                          # 1. load .env
    _k, _, _v = _line.split("#")[0].strip().partition("=")
    if _v:
        os.environ.setdefault(_k.strip(), _v.strip())
from weather_arb.alerts import AlertManager                          # 2. import
from weather_arb.config import Settings
SETTINGS = Settings.from_env()                                       # 3. settings
MESSAGE = "TEST: The lock-in alert pipeline is fully operational."
AlertManager(SETTINGS).send("weather-arb: pipeline test", MESSAGE,   # 4. fire
                            priority="high", tags="white_check_mark")
# -------------------------------------------------------------------------- #

print(f"ntfy_url : {SETTINGS.ntfy_url}")
if not SETTINGS.ntfy_url:
    print("RESULT   : NOT CONFIGURED -- set WA_NTFY_TOPIC in .env "
          "(alerts still land in logs/alerts.log)")
    raise SystemExit(1)

# 5. verify: does the server actually hold what we just sent?
# ntfy's poll endpoint indexes a new message ~2-3 s after accepting it, so a
# single immediate read-back races it and reports a false negative. Retry.
topic = SETTINGS.ntfy_url.rstrip("/").rsplit("/", 1)[-1]
status, hit = None, []
for attempt in range(6):
    if attempt:
        time.sleep(1.5)
    try:
        with urllib.request.urlopen(
                f"https://ntfy.sh/{topic}/json?poll=1&since=120s", timeout=15) as resp:
            status = resp.status
            found = [json.loads(l) for l in resp.read().decode().splitlines() if l.strip()]
    except Exception as exc:  # noqa: BLE001
        print(f"RESULT   : SEND UNVERIFIED -- read-back failed ({exc})")
        raise SystemExit(2)
    hit = [m for m in found
           if m.get("event") == "message" and m.get("message") == MESSAGE]
    if hit:
        break
print(f"HTTP     : {status} on read-back of topic '{topic}'")
if hit:
    print(f"delivered: id={hit[0]['id']}  priority={hit[0].get('priority')}  "
          f"tags={hit[0].get('tags')}")
    print("RESULT   : PIPELINE OPERATIONAL -- ntfy.sh accepted the message.")
    print(f"           If your phone stayed silent, it is not subscribed: open the "
          f"ntfy app -> '+' -> subscribe to the topic '{topic}'.")
else:
    print("RESULT   : SENT BUT NOT FOUND on the server -- check WA_NTFY_URL/topic.")
    raise SystemExit(3)
