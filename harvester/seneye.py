"""Client for the public Seneye cloud API (api.seneye.com/v1).

The public API only ever serves the *last* reading a device uploaded, so this
client is written to be polled on a schedule. It has no third-party
dependencies: everything here is standard library.

Endpoints used
--------------
GET /v1/devices?user=&pwd=&IncludeState=1   list of devices + current state
GET /v1/devices/{id}/exps?user=&pwd=        current experiment values
GET /v1/devices/{id}/state?user=&pwd=       slide serial, expiry, last reading time

Docs: https://api.seneye.com/
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import Any

API_ROOT = "https://api.seneye.com/v1"
USER_AGENT = "tnp-seneye-harvester/1.0 (+https://github.com/Nautilusproject-Gib)"

# What the Seneye actually measures at the nursery. Three things are left out
# on purpose:
#   PAR, lux, colour temperature - the probes sit in sumps, not in lit tanks,
#     so they only ever read zero.
#   O2 - the reef units do not measure dissolved oxygen. Whatever the API
#     returns in that field is not a measurement of this water, and the
#     dashboard shows modelled oxygen at saturation instead (see derived.py).
# NH4 stays as a column because the harvester fills it from the model, having
# first cleared anything the API put there.
PARAMETERS = ("temperature", "ph", "nh3", "nh4")


class SeneyeError(RuntimeError):
    pass


@dataclass
class Reading:
    """One observation from one device at one instant."""

    device_id: str
    reading_time: int  # unix seconds, from the device's last_experiment
    fetched_at: int
    values: dict[str, float] = field(default_factory=dict)
    trends: dict[str, int] = field(default_factory=dict)
    statuses: dict[str, int] = field(default_factory=dict)
    slide_serial: str | None = None
    slide_expires: int | None = None
    out_of_water: int | None = None
    disconnected: int | None = None

    def as_row(self) -> dict[str, Any]:
        row = {
            "device_id": self.device_id,
            "reading_time": self.reading_time,
            "fetched_at": self.fetched_at,
            "slide_serial": self.slide_serial,
            "slide_expires": self.slide_expires,
            "out_of_water": self.out_of_water,
            "disconnected": self.disconnected,
        }
        for p in PARAMETERS:
            row[p] = self.values.get(p)
            row[f"{p}_status"] = self.statuses.get(p)
        return row


@dataclass
class Device:
    device_id: str
    description: str
    type: int | None = None


class SeneyeClient:
    def __init__(self, user: str, pwd: str, timeout: int = 30, retries: int = 3):
        if not user or not pwd:
            raise SeneyeError(
                "Seneye credentials missing. Set SENEYE_USER and SENEYE_PWD."
            )
        self._auth = {"user": user, "pwd": pwd}
        self.timeout = timeout
        self.retries = retries

    # -- transport ---------------------------------------------------------

    def _get(self, path: str, **params: Any) -> Any:
        query = dict(self._auth)
        query.update({k: v for k, v in params.items() if v is not None})
        url = f"{API_ROOT}{path}?{urllib.parse.urlencode(query)}"
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
        )

        last_err: Exception | None = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read().decode("utf-8", "replace")
                return json.loads(body)
            except urllib.error.HTTPError as exc:
                # 401/403 will never succeed on retry; a 429 or 5xx might.
                if exc.code in (401, 403):
                    raise SeneyeError(
                        f"Seneye API rejected the credentials (HTTP {exc.code}) "
                        f"for {path}"
                    ) from exc
                last_err = exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_err = exc
            time.sleep(2 ** attempt)

        raise SeneyeError(f"Seneye API request failed for {path}: {last_err}")

    # -- endpoints ---------------------------------------------------------

    def devices(self) -> list[Device]:
        payload = self._get("/devices")
        if not isinstance(payload, list):
            raise SeneyeError(f"Unexpected /devices payload: {payload!r}")
        return [
            Device(
                device_id=str(d.get("id")),
                description=str(d.get("description") or f"device {d.get('id')}"),
                type=_as_int(d.get("type")),
            )
            for d in payload
        ]

    def devices_with_state(self) -> list[dict[str, Any]]:
        payload = self._get("/devices", IncludeState=1)
        if not isinstance(payload, list):
            raise SeneyeError(f"Unexpected /devices payload: {payload!r}")
        return payload

    def state(self, device_id: str) -> dict[str, Any]:
        return self._get(f"/devices/{device_id}/state")

    def exps(self, device_id: str) -> dict[str, Any]:
        return self._get(f"/devices/{device_id}/exps")

    # -- high level --------------------------------------------------------

    def poll(self) -> list[Reading]:
        """One pass over every device on the account.

        Uses the combined /devices?IncludeState=1 call so a poll costs one
        request rather than three per device.
        """
        now = int(time.time())
        readings: list[Reading] = []
        for dev in self.devices_with_state():
            device_id = str(dev.get("id"))
            status = dev.get("status") or {}
            exps = dev.get("exps") or {}
            reading_time = _as_int(status.get("last_experiment")) or now
            readings.append(
                parse_reading(
                    device_id=device_id,
                    exps=exps,
                    status=status,
                    reading_time=reading_time,
                    fetched_at=now,
                )
            )
        return readings


def parse_reading(
    device_id: str,
    exps: dict[str, Any],
    status: dict[str, Any],
    reading_time: int,
    fetched_at: int,
) -> Reading:
    """Turn the API's nested exps/status blocks into a flat Reading.

    Kept as a free function so it can be unit-tested against recorded payloads
    without touching the network.
    """
    values: dict[str, float] = {}
    trends: dict[str, int] = {}
    statuses: dict[str, int] = {}

    for name in PARAMETERS:
        block = exps.get(name)
        if not isinstance(block, dict):
            continue
        curr = _as_float(block.get("curr"))
        if curr is None:
            continue
        values[name] = curr
        trend = _as_int(block.get("trend"))
        if trend is not None:
            trends[name] = trend
        st = _as_int(block.get("status"))
        if st is not None:
            statuses[name] = st

    return Reading(
        device_id=device_id,
        reading_time=reading_time,
        fetched_at=fetched_at,
        values=values,
        trends=trends,
        statuses=statuses,
        slide_serial=(str(status["slide_serial"]) if status.get("slide_serial") else None),
        slide_expires=_as_int(status.get("slide_expires")),
        out_of_water=_as_int(status.get("out_of_water")),
        disconnected=_as_int(status.get("disconnected")),
    )


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
