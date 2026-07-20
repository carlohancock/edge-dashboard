"""Shared helper: build a sleeper_id -> nflverse gsis_id lookup.

Used by both player_crosswalk_report.py (Step 2, dry-run coverage report)
and backfill_player_game_stats_2025.py (Step 3, actual stats backfill) so
the matching logic only lives in one place.
"""

from __future__ import annotations

import os
from typing import Any

# Fix a common macOS python.org SSL issue (missing local CA bundle) before
# nfl_data_py/pandas make any HTTPS requests. Must happen before those
# imports touch the network.
import certifi

os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("SSL_CERT_DIR", os.path.dirname(certifi.where()))

import nfl_data_py as nfl  # noqa: E402


def build_sleeper_to_gsis_lookup() -> dict[str, dict[str, Any]]:
    """sleeper_id (str) -> {gsis_id, pfr_id, nflverse_name} from nflverse's crosswalk."""
    crosswalk = nfl.import_ids(columns=["sleeper_id", "gsis_id", "pfr_id", "name"])
    crosswalk = crosswalk[crosswalk["sleeper_id"].notna()].copy()

    # sleeper_id arrives as a float (e.g. 13269.0); normalize to the same
    # string form Sleeper/our players table uses ("13269").
    crosswalk["sleeper_id"] = crosswalk["sleeper_id"].apply(lambda x: str(int(x)))

    # A handful of retired/obscure players share a sleeper_id in this dataset
    # (data-quality quirk upstream, not ours). Prefer the row that actually
    # has a gsis_id so downstream joins have something usable.
    crosswalk["has_gsis"] = crosswalk["gsis_id"].notna()
    crosswalk = crosswalk.sort_values("has_gsis", ascending=False)
    crosswalk = crosswalk.drop_duplicates(subset="sleeper_id", keep="first")

    lookup: dict[str, dict[str, Any]] = {}
    for row in crosswalk.itertuples(index=False):
        lookup[row.sleeper_id] = {
            "gsis_id": row.gsis_id if isinstance(row.gsis_id, str) else None,
            "pfr_id": row.pfr_id if isinstance(row.pfr_id, str) else None,
            "nflverse_name": row.name,
        }
    return lookup
