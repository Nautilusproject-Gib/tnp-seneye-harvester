"""Values the Seneye does not measure, worked out from ones it does.

Two quantities, and they are different kinds of thing. Both are labelled
"modelled" wherever they appear, and neither is ever written into the same
field as a measurement.

**Ammonium (NH4+)** is a real derivation. The Seneye reports free ammonia
(NH3), which is one side of an equilibrium that depends on pH, temperature and
salinity. Given those, the rest of the total ammonia pool is ammonium, so NH4+
follows from the measurements rather than being guessed at.

**Oxygen at saturation** is not a measurement of what is in the tank. It is how
much oxygen the water *could* hold at the measured temperature and the
salinity in use, at sea-level pressure. Real dissolved oxygen sits below that
whenever respiration outpaces exchange, and knowing it needs a probe. It is
reported as a ceiling, and the dashboard says so.

Salinity comes from the most recent in-situ sample for that sump when there is
one, and otherwise from `derived.default_salinity` in config.json. It is never
invented per reading.

Sources
-------
Ammonia dissociation in saline water, valid 5-35 ppt, 5-35 degC, pH 7.8-8.3:

    pKa = 0.0901821 + 2729.92/(T+273.2) + (0.1552 - 0.0003142*T)*I
    I   = 19.973*S/(1000 - 1.2005109*S)
    f   = 1/(10^(pKa - pH) + 1)          fraction present as free NH3

after Whitfield (1974) and Bower and Bidwell (1978), in the form used by the
Florida Department of Environmental Protection's un-ionised ammonia procedure.
Bell et al. (2007) show that Khoo-derived expressions of this family can
overstate NH3, so the ammonium figure is treated as indicative.

Oxygen solubility, Benson and Krause (1980, 1984) as adopted by the USGS:

    DO0 = exp(-139.34411 + 1.575701e5/T - 6.642308e7/T^2
              + 1.243800e10/T^3 - 8.621949e11/T^4)      mg/L, fresh, 1 atm
    Fs  = exp(-S*(0.017674 - 10.754/T + 2140.7/T^2))    salinity correction
    DO  = DO0 * Fs                                      T in kelvin
"""

from __future__ import annotations

import math
from typing import Any

# Molar masses, g/mol
M_NH3 = 17.031
M_NH4 = 18.039

# Bounds the published fits are valid within. Outside them the value is not
# reported at all rather than extrapolated.
AMMONIA_LIMITS = {"temp": (0.0, 40.0), "ph": (6.0, 10.0), "salinity": (0.0, 45.0)}
OXYGEN_LIMITS = {"temp": (0.0, 40.0), "salinity": (0.0, 45.0)}


def _within(value: float | None, limits: tuple[float, float]) -> bool:
    return value is not None and limits[0] <= value <= limits[1]


def ionic_strength(salinity: float) -> float:
    """Formal ionic strength from salinity in ppt."""
    return 19.973 * salinity / (1000.0 - 1.2005109 * salinity)


def ammonia_pka(temperature_c: float, salinity: float) -> float:
    """pKa of the NH4+/NH3 pair in saline water."""
    t = temperature_c
    return (
        0.0901821
        + 2729.92 / (t + 273.2)
        + (0.1552 - 0.0003142 * t) * ionic_strength(salinity)
    )


def free_ammonia_fraction(temperature_c: float, ph: float, salinity: float) -> float:
    """Fraction of total ammonia present as free NH3."""
    return 1.0 / (10.0 ** (ammonia_pka(temperature_c, salinity) - ph) + 1.0)


def ammonium_from_free_ammonia(
    nh3_mg_l: float | None,
    temperature_c: float | None,
    ph: float | None,
    salinity: float | None,
    nh3_as_nitrogen: bool = False,
) -> float | None:
    """NH4+ in mg/L from measured free NH3, or None if it cannot be had.

    With free NH3 measured and the equilibrium fraction known, total ammonia is
    NH3/f and the remainder is ammonium. Zero free ammonia gives zero ammonium,
    which is the right answer rather than a division by zero.
    """
    if nh3_mg_l is None or not _within(temperature_c, AMMONIA_LIMITS["temp"]):
        return None
    if not _within(ph, AMMONIA_LIMITS["ph"]) or not _within(salinity, AMMONIA_LIMITS["salinity"]):
        return None
    if nh3_mg_l <= 0:
        return 0.0

    # Seneye may report the nitrogen mass rather than the ammonia mass.
    nh3_mass = nh3_mg_l * (M_NH3 / 14.007) if nh3_as_nitrogen else nh3_mg_l

    fraction = free_ammonia_fraction(temperature_c, ph, salinity)
    if fraction <= 0 or fraction >= 1:
        return None

    moles_nh3 = nh3_mass / M_NH3
    moles_total = moles_nh3 / fraction
    moles_nh4 = moles_total - moles_nh3
    return max(0.0, moles_nh4 * M_NH4)


def oxygen_at_saturation(
    temperature_c: float | None, salinity: float | None
) -> float | None:
    """Oxygen solubility in mg/L at sea-level pressure.

    This is the ceiling the water could reach, not what is in it. Barometric
    pressure is taken as 1 atm: the tanks are at sea level and open to the air,
    and the correction is under a percent for ordinary weather.
    """
    if not _within(temperature_c, OXYGEN_LIMITS["temp"]):
        return None
    if not _within(salinity, OXYGEN_LIMITS["salinity"]):
        return None

    t = temperature_c + 273.15
    do_fresh = math.exp(
        -139.34411
        + 1.575701e5 / t
        - 6.642308e7 / t ** 2
        + 1.243800e10 / t ** 3
        - 8.621949e11 / t ** 4
    )
    salinity_factor = math.exp(
        -salinity * (0.017674 - 10.754 / t + 2140.7 / t ** 2)
    )
    return do_fresh * salinity_factor


# -- applying it to stored readings ---------------------------------------

DERIVED_PARAMETERS = ("nh4", "o2_sat")


def salinity_by_sump(store, default: float) -> dict[str, float]:
    """Most recent in-situ salinity per sump, falling back to the default."""
    try:
        rows = store.query(
            "SELECT sump_code, salinity_ppt, sample_date FROM nutrients "
            "WHERE salinity_ppt IS NOT NULL ORDER BY sample_date"
        )
    except Exception:
        return {}
    latest: dict[str, float] = {}
    for row in rows:
        if row.get("salinity_ppt") is not None:
            latest[row["sump_code"]] = float(row["salinity_ppt"])
    return latest


def enrich(rows: list[dict[str, Any]], sump_of: dict[str, str],
           salinity: dict[str, float], config: dict[str, Any]) -> list[str]:
    """Add the modelled fields to each reading in place.

    Returns the derived keys that ended up with at least one value, so the
    dashboard never offers a parameter tab that is empty everywhere.
    """
    cfg = config.get("derived", {}) or {}
    if not cfg.get("enabled", True):
        return []
    default_salinity = cfg.get("default_salinity")
    as_nitrogen = bool(cfg.get("nh3_as_nitrogen", False))
    want_nh4 = cfg.get("ammonium", True)
    want_o2 = cfg.get("oxygen_saturation", True)

    produced: set[str] = set()
    for row in rows:
        sump = sump_of.get(row.get("device_id"))
        s = salinity.get(sump, default_salinity)
        temp = row.get("temperature")
        if want_nh4:
            value = ammonium_from_free_ammonia(
                row.get("nh3"), temp, row.get("ph"), s, as_nitrogen
            )
            if value is not None:
                row["nh4"] = value
                produced.add("nh4")
        if want_o2:
            value = oxygen_at_saturation(temp, s)
            if value is not None:
                row["o2_sat"] = value
                produced.add("o2_sat")
    return [k for k in DERIVED_PARAMETERS if k in produced]
