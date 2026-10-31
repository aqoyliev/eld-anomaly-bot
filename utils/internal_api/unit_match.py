"""Which Vitality vehicles could be a truck, by unit number.

The same rules the driver-alerts bot (nexus-fleet) used against DriveHOS before
it moved to this API (its utils/unit_key.py and utils/vitality.lookup_all), so
the switch changes nothing in what counts as a match:

  * the exact match: the unit's normalized key (letters and digits only,
    upper-case), then the bare unit before a suffix ("4063 O/O" -> "4063"),
    each also tried without leading zeros;
  * then every OTHER vehicle that is a variant of the same unit: the same
    first token without trailing letters ("4063" and "4063z", "4063 O/O").

More than one match is ambiguous and the caller must tell them apart (by
position, say) or name nobody. This is deliberately NOT the poller's
quantum_key, which folds "4063z" into "4063" and keeps only one of them.
"""

import re
from typing import Dict, List, Optional, Tuple

_NON_ALNUM = re.compile(r"[^0-9A-Z]")
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

EXACT = "exact"
VARIANT = "variant"


def normalize_unit(value: Optional[str]) -> str:
    return _NON_ALNUM.sub("", (value or "").upper())


def unit_candidates(value: Optional[str]) -> List[str]:
    """Keys to try, in order: the full key, then the first whitespace token
    when it holds a digit and differs ("4063 O/O" -> "4063"; "O/O 4063" has no
    fallback)."""
    full = normalize_unit(value)
    candidates = [full]
    tokens = (value or "").split()
    if len(tokens) > 1 and any(ch.isdigit() for ch in tokens[0]):
        candidates.append(normalize_unit(tokens[0]))
    return list(dict.fromkeys(c for c in candidates if c))


def unit_base(value: Optional[str]) -> str:
    """The bare unit a vehicle name is a variant of: its first token,
    normalized, without trailing letters and leading zeros ("4063z" -> "4063",
    "4063 O/O" -> "4063", "T-120" -> "T120"). A name that would lose all its
    digits keeps them."""
    tokens = (value or "").split()
    key = normalize_unit(tokens[0] if tokens else "")
    base = key.rstrip(_LETTERS)
    if not any(ch.isdigit() for ch in base):
        base = key
    return base.lstrip("0") or base


def _number(row: dict) -> str:
    return str(row.get("number") or "").strip()


def match_rows(rows: List[dict], query: Optional[str]) -> List[Tuple[dict, str]]:
    """Every latest-vehicle-status row that could be the truck `query`, as
    (row, EXACT | VARIANT): the exact match first, then the variants in feed
    order. Rows without a number are ignored."""
    index: Dict[str, dict] = {}
    pool: List[dict] = []
    for row in rows:
        key = normalize_unit(_number(row))
        if not key:
            continue
        pool.append(row)
        index.setdefault(key, row)
        index.setdefault(key.lstrip("0") or key, row)

    found: List[Tuple[dict, str]] = []
    for key in unit_candidates(query):
        row = index.get(key) or index.get(key.lstrip("0") or key)
        if row is not None:
            found.append((row, EXACT))
            break
    base = unit_base(query)
    if not base:
        return found
    for row in pool:
        if all(row is not f for f, _ in found) and unit_base(_number(row)) == base:
            found.append((row, VARIANT))
    return found
