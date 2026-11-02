"""consignment_payouts: deterministic consignment/booth payout splitting.

Copyright (c) 2026 Asdale Dynamics, LLC. MIT licensed.
"""

__version__ = "1.0.0"

from .core import (  # noqa: F401
    CommissionTier,
    ItemLine,
    VendorSummary,
    load_config,
    parse_config,
    load_items,
    compute_settlement,
    write_official_csv,
    write_summary_csv,
)

__all__ = [
    "CommissionTier",
    "ItemLine",
    "Vendor",
    "VendorSummary",
    "load_config",
    "parse_config",
    "load_items",
    "compute_settlement",
    "write_official_csv",
    "write_summary_csv",
]
