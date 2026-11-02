# Consignment Payout Splitter (CLI)

Compute **per-vendor consignment/booth payouts** from a simple sales CSV: commission tiers, per-item discounts, flat fees, and deductions — plus a settlement report and per-vendor summary, all in plain CSV. Perfect for craft-fair vendors, maker markets, and small shops that split sales with the booth host.

Built and maintained by **Asdale Dynamics, LLC** as part of a beginner-friendly collection of practical business tools. Plain English, step by step, no spreadsheets required.

- Python 3.10+ standard library only — no dependencies, nothing to install
- Local-first: your sales data never leaves your machine; no network calls, no secrets
- Fully deterministic money math (exact integer cents, `ROUND_HALF_UP` at documented points)
- MIT licensed — use it, sell with it, change it

## Quick start

```bash
# run it on the bundled example
python3 -m consignment_payouts examples/sample_items.csv

# use your own commission tiers
python3 -m consignment_payouts my_sales.csv --config my_tiers.json

# write the per-item settlement + per-vendor summary CSVs
python3 -m consignment_payouts my_sales.csv -o settlement.csv --summary-out summary.csv
```

Output (to stdout) is a readable report; with `-o`/`--summary-out` you also get the CSV files. Use `--quiet` to skip the console report.

## Input CSV format

| column | meaning |
|---|---|
| `vendor_id` | short code for the seller (e.g. `V101`) |
| `vendor_name` | display name |
| `item_id` | unique item code within the vendor |
| `description` | human-readable description |
| `qty` | units sold (**must be a positive whole number**) |
| `unit_price` | pre-tax price per unit, e.g. `18.00` |
| `discount_pct` | percent off, e.g. `10` for 10% off |
| `fee` | flat fee charged to the vendor for the item (e.g. booth share), deducted from payout |
| `deduction` | extra deduction (repair, marketing, etc.), deducted from payout |
| `tax` | sales tax collected — **informational only**, never affects payout |

See [`examples/sample_items.csv`](examples/sample_items.csv) and [`examples/edge_case_items.csv`](examples/edge_case_items.csv).

## Tier config

A tiers JSON defines commission bands by the item's **net sale** (after discount):

```json
{
  "tiers": [
    { "name": "booth",    "rate_pct": 5,  "min_net_sale": 0,      "max_net_sale": 49.99 },
    { "name": "standard", "rate_pct": 15, "min_net_sale": 50,     "max_net_sale": 199.99 },
    { "name": "premium",  "rate_pct": 25, "min_net_sale": 200,    "max_net_sale": null }
  ],
  "tier_overrides": { "V101": "booth" }
}
```

- `max_net_sale: null` means "and up"; only the **last** tier may be uncapped.
- `tier_overrides` (optional) force a tier by `item_id` or `vendor_id`; every key must exist in the CSV and every tier named must exist above.

## How the money math works (deterministic, documented)

All amounts are converted to **integer cents** up front and never held as floats. Rounding is `ROUND_HALF_UP` (0.5 always rounds up; for negatives, away from zero) at exactly these points:

1. `gross = qty × unit_price` — exact
2. `discount = ROUND_HALF_UP(gross × discount_pct / 100)`
3. `net_sale = gross − discount` — exact
4. `commission = ROUND_HALF_UP(net_sale × tier_rate_pct / 100)`
5. `payout = net_sale − commission − fee − deduction` — exact

Vendor summaries sum exact integer cents, so no rounding error accumulates. Sales tax is carried through as information only. The same input always produces the same output — recomputing a settled show always matches the original report.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

29 tests cover the payout engine, half-cent rounding edges, tier selection and overrides, input validation, both report CSVs, and the CLI end to end against the example data.

## Project layout

```
consignment_payouts/    engine, CLI (python package)
examples/               sample + edge-case settlement CSVs, tiers.json
tests/                  unittest suite
```

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Asdale Dynamics, LLC.

*Educational tool, not accounting or tax advice. Check your market's consignment agreement and your tax rates before paying out.*
