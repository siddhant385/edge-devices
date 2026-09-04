"""Convert Degrees-Minutes-Seconds (DMS) coordinates to decimal degrees.

Usage:
    python utils/dms_to_decimal.py "30°51'00\"N" "72°20'42\"E"
    30.85 72.345

Accepts these DMS forms:
    30°51'00"N     30 51 00 N     30-51-00N     30.85N
    72°20'42"E     72 20 42 E     72-20-42E     72.345

Output: <latitude> <longitude>  (one line, space-separated)
Exit code 0 on success, 1 on parse error.
"""

from __future__ import annotations

import re
import sys
from typing import Tuple


_HEMISPHERE_SIGN = {"N": 1, "S": -1, "E": 1, "W": -1}


def _parse_dms(s: str) -> float:
    """Parse one DMS string into decimal degrees. Sign comes from the
    trailing hemisphere letter (N/S/E/W). Bare decimal is also accepted.
    """
    s = s.strip()
    if not s:
        raise ValueError("empty coordinate")

    # Extract hemisphere if present; default to +1 (N/E) if absent.
    sign = 1
    if s[-1].upper() in _HEMISPHERE_SIGN:
        sign = _HEMISPHERE_SIGN[s[-1].upper()]
        s = s[:-1].strip()

    # Split on any non-digit separator (°, ', ", space, dash).
    # The remaining tokens are degrees, minutes, seconds.
    tokens = re.findall(r"\d+(?:\.\d+)?", s)
    if not tokens:
        raise ValueError(f"no numbers found in {s!r}")
    if len(tokens) > 3:
        raise ValueError(f"too many numeric groups in {s!r}: {tokens}")

    degrees = float(tokens[0])
    minutes = float(tokens[1]) if len(tokens) >= 2 else 0.0
    seconds = float(tokens[2]) if len(tokens) == 3 else 0.0
    if minutes >= 60 or seconds >= 60:
        raise ValueError(
            f"minutes/seconds out of range in {s!r} (got {minutes}m {seconds}s)"
        )

    decimal = degrees + minutes / 60.0 + seconds / 3600.0
    return sign * decimal


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "usage: dms_to_decimal.py <lat_dms> <lon_dms>\n"
            "  example: dms_to_decimal.py '30°51\\'00\"N' '72°20\\'42\"E'",
            file=sys.stderr,
        )
        return 1
    try:
        lat = _parse_dms(argv[1])
        lon = _parse_dms(argv[2])
    except ValueError as e:
        print(f"parse error: {e}", file=sys.stderr)
        return 1
    if not -90.0 <= lat <= 90.0:
        print(f"latitude out of range: {lat}", file=sys.stderr)
        return 1
    if not -180.0 <= lon <= 180.0:
        print(f"longitude out of range: {lon}", file=sys.stderr)
        return 1
    print(f"{lat} {lon}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
