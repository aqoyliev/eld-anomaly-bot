"""Live check for the VIN-disambiguation fix (same unit number, different truck).

For a Samsara fleet: pull the moving vehicles, enrich them with VINs via the new
samsara.fetch_vins (/fleet/vehicles), then look each unit up in Quantum and
compare VINs the way detector.vins_conflict does. Prints, per moving unit,
whether the provider VIN and the Quantum VIN agree, disagree (= a real
unit-number collision we now skip), or are missing.

Run from the project root:
    .venv/Scripts/python.exe scripts/verify_vin_match.py \
        --samsara <samsara-token> --quantum <quantum-token>
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import config  # noqa: E402
from utils.eld import quantumeld, samsara  # noqa: E402
from utils.eld.detector import vins_conflict  # noqa: E402


async def run(samsara_token: str, quantum_token: str) -> int:
    if not samsara_token or not quantum_token:
        print("Need both --samsara and --quantum tokens.")
        return 1

    # All vehicles with a speed reading (threshold -1 lists parked too), so the
    # check covers the whole fleet, not just the few moving right now.
    vehicles = await samsara.fetch_moving_vehicles(
        samsara_token, config.SAMSARA_BASE_URL, threshold_mph=-1.0
    )
    print(f"Samsara vehicles with a GPS reading: {len(vehicles)}")

    vins = await samsara.fetch_vins(
        [v.vehicle_id for v in vehicles.values() if v.vehicle_id],
        samsara_token, config.SAMSARA_BASE_URL,
    )
    print(f"VINs returned by /fleet/vehicles: {len(vins)}\n")

    quantum = await quantumeld.fetch_vehicles(
        list(vehicles.keys()), quantum_token, config.QUANTUM_BASE_URL
    )

    conflicts = matches = missing = not_in_quantum = 0
    for unit, v in sorted(vehicles.items()):
        v_vin = vins.get(v.vehicle_id) if v.vehicle_id else None
        q = quantum.get(unit)
        if q is None:
            not_in_quantum += 1
            continue
        if not v_vin or not q.vin:
            tag, missing = "VIN MISSING (falls back to unit match)", missing + 1
        elif vins_conflict(v_vin, q.vin):
            tag, conflicts = "*** CONFLICT — different truck, now SKIPPED ***", conflicts + 1
        else:
            tag, matches = "match", matches + 1
        print(f"  {unit:<12} samsara={v_vin}  quantum={q.vin}  -> {tag}")

    print(
        f"\nSummary: {matches} match, {conflicts} conflict (skipped), "
        f"{missing} missing-VIN, {not_in_quantum} not in Quantum."
    )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Verify the VIN-disambiguation fix live.")
    p.add_argument("--samsara", default=os.environ.get("SAMSARA_TOKEN", ""))
    p.add_argument("--quantum", default=os.environ.get("QUANTUM_TOKEN", ""))
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args.samsara, args.quantum)))


if __name__ == "__main__":
    main()
