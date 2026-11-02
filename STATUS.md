# STATUS.md — consignment-payout-splitter-cli

Last updated: 2026-11-02

## What was built

A zero-dependency (Python 3.10+ stdlib) CLI that splits craft-fair/market
sales into **per-vendor consignment payouts**:

- Per-item engine in integer cents: `gross → percent discount → net sale →
  commission by tier → payout (net_sale − commission − fee − deduction)`.
- Sales tax is carried as informational pass-through only; it never alters payouts.
- Configurable commission tiers by net-sale band (default `booth` 5% /
  `standard` 15% / `premium` 25%), with optional per `item_id`/`vendor_id`
  tier overrides.
- Outputs: human-readable console report, per-item settlement CSV (`-o`),
  per-vendor summary CSV (`--summary-out`); totals row included in both.
- Strict input validation: required columns, positive qty, non-negative
  price, duplicate `(vendor_id, item_id)` rejection, tier-range overlap
  detection, unknown tier-name/override-key rejection.
- Fully deterministic money math, documented in the README and in the
  module docstring of `consignment_payouts/core.py`: all values are integer
  cents; `ROUND_HALF_UP` is applied at exactly two points (discount and
  commission); everything else is exact. Same input ⇒ identical report.
- Local-first: no network calls, no secrets, no personal data. MIT licensed,
  Copyright (c) 2026 Asdale Dynamics, LLC.

## How to run it

```bash
# from this folder
python3 -m consignment_payouts examples/sample_items.csv                 # console report + CSV
python3 -m consignment_payouts my_sales.csv --config my_tiers.json      # custom tiers
python3 -m consignment_payouts my_sales.csv -o settlement.csv --summary-out summary.csv
python3 -m consignment_payouts --version / --help
```

Input columns: `vendor_id, vendor_name, item_id, description, qty,
unit_price, discount_pct, fee, deduction, tax` (see `examples/sample_items.csv`).
Default output file: `<input>_payout_report.csv` next to the input CSV.

## Verification results (2026-11-02)

- `python3 -m unittest discover -s tests` → **Ran 29 tests — OK** (all green)
- `python3 -m compileall consignment_payouts` → clean compile
- CLI smoke test on `examples/sample_items.csv`: payout $99.17 (V101),
  grand total payout $425.82 — matches hand-computed expectations asserted
  in the test suite
- CLI smoke test on `examples/edge_case_items.csv`: premium-tier item
  ($320.00, 25% → $80.00 commission) and half-cent rounding edges verified
  (e.g. $90.30 × 15% = $13.545 → $13.55 HALF_UP)
- `python3 -m consignment_payouts --version` → `consignment-payouts 1.0.0`
- Error paths verified in tests: missing file (exit 1), missing column,
  empty CSV, zero qty, negative price, unknown tier override
- Git: single repo initialized locally; committed (`a1ab0f1`); not pushed,
  no remote, per instructions

## Known gaps / next steps

- Tier bands are matched on discounted net sale only; no support for
  percentage fees or per-vendor monthly rental amortization yet.
- No payment-memo/checkout split export (e.g. per-vendor "how this was
  calculated" PDF/HTML); CSV only for now.
- Caveat for users: this is an educational tool, not accounting or tax
  advice — noted in the README.
- Percentage amounts are parsed with `decimal.Decimal` at full precision;
  very long decimal inputs (more than two decimals on prices) are not
  explicitly rejected and round at the cents conversion, which could
  surprise users. A stricter currency parser would be a small improvement.
