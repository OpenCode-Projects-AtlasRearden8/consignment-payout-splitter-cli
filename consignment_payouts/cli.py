"""Command-line interface for consignment payouts."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import (
    compute_settlement,
    format_report,
    load_config,
    load_items,
    write_official_csv,
    write_summary_csv,
)

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="consignment-payouts",
        description=(
            "Compute per-vendor consignment/booth payouts from an item sales CSV. "
            "Applies commission tiers, discounts, flat fees, and deductions per item. "
            "Sales tax is carried through as information only (never part of payout). "
            "All money math is exact integer cents with ROUND_HALF_UP at documented "
            "rounding points; results are fully deterministic."
        ),
    )
    p.add_argument("items_csv", nargs="?", help="input CSV of sold items (see examples/sample_items.csv)")
    p.add_argument(
        "--config",
        default=None,
        help="optional tiers JSON (default: built-in tiers matching examples/tiers.json)",
    )
    p.add_argument(
        "-o",
        "--out",
        default=None,
        help="write per-item settlement CSV to this path (default: <items>_settlement.csv)",
    )
    p.add_argument(
        "--summary-out",
        default=None,
        help="also write a per-vendor summary CSV (default: none)",
    )
    p.add_argument(
        "--quiet", action="store_true", help="do not print the human-readable report to stdout"
    )
    p.add_argument(
        "--version", action="store_true", help="print version and exit"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        from . import __version__

        print(f"consignment-payouts {__version__}")
        return 0

    if not args.items_csv:
        parser.error("the following arguments are required: items_csv")

    try:
        if args.config:
            tiers_config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        else:
            tiers_config = {
                "tiers": [
                    {"name": "booth", "rate_pct": 5, "min_net_sale": 0, "max_net_sale": 49.99},
                    {"name": "standard", "rate_pct": 15, "min_net_sale": 50, "max_net_sale": 199.99},
                    {"name": "premium", "rate_pct": 25, "min_net_sale": 200, "max_net_sale": None},
                ]
            }
        from .core import parse_config

        tiers, _ = parse_config(tiers_config)

        rows = load_items(args.items_csv)
        settlement = compute_settlement(rows, tiers, tiers_config.get("tier_overrides") or {})
    except (ValueError, OSError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out_path = args.out or str(Path(args.items_csv).with_name(Path(args.items_csv).stem + "_payout_report.csv"))
    write_official_csv(settlement, out_path)

    if args.summary_out:
        write_summary_csv(settlement, args.summary_out)

    if not args.quiet:
        print(format_report(settlement))
        print()
        print(f"per-item report written to: {out_path}")
        if args.summary_out:
            print(f"summary report written to: {args.summary_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
