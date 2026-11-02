"""Tests for consignment_payouts core engine and CLI.

Run with:  python3 -m unittest discover -s tests -v
(or: python3 -m unittest tests.test_engine -v)
"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from consignment_payouts.core import (  # noqa: E402
    _round,
    _to_cents,
    _cents_to_str,
    compute_settlement,
    load_config,
    load_items,
    parse_config,
    write_official_csv,
    write_summary_csv,
)
from consignment_payouts.cli import main as cli_main  # noqa: E402

EXAMPLES = REPO / "examples"
SAMPLE_ITEMS = EXAMPLES / "sample_items.csv"
EDGE_ITEMS = EXAMPLES / "edge_case_items.csv"
TIERS = EXAMPLES / "tiers.json"


def _read_csv(path: str) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


class MoneyHelpers(unittest.TestCase):
    def test_to_cents(self):
        self.assertEqual(_to_cents("19.99"), 1999)
        self.assertEqual(_to_cents("0.00"), 0)
        self.assertEqual(_to_cents(" 5 "), 500)
        self.assertEqual(_to_cents("10.5"), 1050)

    def test_round_half_up(self):
        # _round takes a Decimal count of CENTS; .5 cent and above rounds up.
        self.assertEqual(_round(Decimal("2.5")), 3)
        self.assertEqual(_round(Decimal("2.4999")), 2)
        self.assertEqual(_round(Decimal("0.5")), 1)
        # HALF_UP for negatives rounds away from zero (deterministic, documented).
        self.assertEqual(_round(Decimal("-0.5")), -1)
        self.assertEqual(_round(Decimal("-1.5")), -2)

    def test_cents_to_str(self):
        self.assertEqual(_cents_to_str(1999), "19.99")
        self.assertEqual(_cents_to_str(0), "0.00")
        self.assertEqual(_cents_to_str(-5), "-0.05")


class ConfigTests(unittest.TestCase):
    def test_load_example_tiers(self):
        tiers, default = load_config(TIERS)
        self.assertEqual([t.name for t in tiers], ["booth", "standard", "premium"])
        self.assertEqual(tiers[0].rate_pct, Decimal("5"))
        self.assertEqual(tiers[1].min_net_sale, Decimal(50))
        self.assertIsNone(tiers[2].max_net_sale)
        self.assertEqual(default, Decimal("5"))

    def test_parse_config_rejects_empty(self):
        with self.assertRaises(ValueError):
            parse_config({})

    def test_parse_config_rejects_overlap(self):
        raw = {
            "tiers": [
                {"name": "a", "rate_pct": 5, "min_net_sale": 0, "max_net_sale": 100},
                {"name": "b", "rate_pct": 7, "min_net_sale": 50},
            ]
        }
        with self.assertRaises(ValueError):
            parse_config(raw)


class SampleSettlementTests(unittest.TestCase):
    """Payouts hand-computed from examples/tiers.json."""

    @classmethod
    def setUpClass(cls):
        rows = load_items(SAMPLE_ITEMS)
        tiers, _ = load_config(TIERS)
        cls.settlement = compute_settlement(rows, tiers)
        cls.items = {i.item_id: i for i in cls.settlement.items}

    def test_expected_row_count_and_vendors(self):
        self.assertEqual(len(self.settlement.items), 8)
        self.assertEqual(len(self.settlement.vendors), 4)

    def testPrairieRose_pottery(self):
        p1 = self.items["P001"]
        self.assertEqual(p1.gross_cents, 7200)
        self.assertEqual(p1.net_sale_cents, 7200)
        self.assertEqual(p1.commission_cents, 1080)  # standard 15%
        self.assertEqual(p1.payout_cents, 6070)      # 72.00 - 10.80 - 0.50

        p2 = self.items["P002"]
        self.assertEqual(p2.gross_cents, 4500)
        self.assertEqual(p2.discount_cents, 450)     # 10%
        self.assertEqual(p2.commission_cents, 203)   # 5% of 40.50 = 2.025 -> 2.03
        self.assertEqual(p2.payout_cents, 3847)

    def test_cedar_barn(self):
        c10 = self.items["C010"]
        self.assertEqual(c10.commission_cents, 1305)
        self.assertEqual(c10.payout_cents, 7345)

        c11 = self.items["C011"]
        self.assertEqual(c11.commission_cents, 220)
        self.assertEqual(c11.payout_cents, 4180)

    def test_willow_creek(self):
        w6 = self.items["W006"]
        self.assertEqual(w6.net_sale_cents, 5700)    # 60.00 - 5% = 57.00
        self.assertEqual(w6.commission_cents, 855)
        self.assertEqual(w6.payout_cents, 4795)      # 57.00 - 8.55 - 0.50

        w5 = self.items["W005"]
        self.assertEqual(w5.payout_cents, 6875)      # 85 − 12.75 − 1.00 − 2.50

    def test_vendors_and_grand_totals(self):
        v101 = next(v for v in self.settlement.vendors if v.vendor_id == "V101")
        self.assertEqual(v101.payout_cents, 9917)

        self.assertEqual(self.settlement.grand_net_sale_cents, 49750)
        self.assertEqual(self.settlement.grand_commission_cents, 6618)
        self.assertEqual(self.settlement.grand_payout_cents, 42582)

        # Payout + commission = net_sale + fees-retained? No: payout = net - comm - fee - ded
        self.assertEqual(
            self.settlement.grand_payout_cents,
            self.settlement.grand_net_sale_cents
            - self.settlement.grand_commission_cents
            - self.settlement.grand_fee_cents
            - self.settlement.grand_deduction_cents,
        )

    def test_tax_is_passthrough_only(self):
        totals_tax = self.settlement.grand_tax_cents
        self.assertEqual(totals_tax, 1868)

    def test_deterministic(self):
        rows = load_items(SAMPLE_ITEMS)
        tiers, _ = load_config(TIERS)
        again = compute_settlement(rows, tiers)
        self.assertEqual(
            [(i.item_id, i.payout_cents) for i in self.settlement.items],
            [(i.item_id, i.payout_cents) for i in again.items],
        )


class EdgeCaseTests(unittest.TestCase):
    """Exercises premium tier, discount, deduction, and half-cent rounding."""

    @classmethod
    def setUpClass(cls):
        rows = load_items(EDGE_ITEMS)
        tiers, _ = load_config(TIERS)
        cls.settlement = compute_settlement(rows, tiers)
        cls.items = {i.item_id: i for i in cls.settlement.items}

    def test_premium_tier(self):
        q = self.items["Q100"]
        self.assertEqual(q.tier_name, "premium")
        self.assertEqual(q.commission_cents, 8000)   # 25% of 320.00
        self.assertEqual(q.payout_cents, 24000)

    def test_standard_half_cent_round_up(self):
        s = self.items["S001"]
        # 90.30 * 15% = 13.545 -> 13.55 (HALF_UP at the half-cent point)
        self.assertEqual(s.commission_cents, 1355)
        self.assertEqual(s.payout_cents, 7625)

    def test_discount_into_booth_tier(self):
        t = self.items["T002"]
        # 25.00 - 15% = 21.25 -> booth 5% = 1.0625 -> 1.06
        self.assertEqual(t.net_sale_cents, 2125)
        self.assertEqual(t.commission_cents, 106)
        self.assertEqual(t.payout_cents, 1984)

    def test_booth_half_cent_round_up(self):
        k = self.items["K010"]
        # 24.75 * 5% = 1.2375 -> 1.24
        self.assertEqual(k.commission_cents, 124)
        self.assertEqual(k.payout_cents, 2326)       # - 0.25 deduction


class InputValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tiers, _ = load_config(TIERS)

    def _rows(self, **over):
        rows = load_items(SAMPLE_ITEMS)
        rows[0].update({k: v for k, v in over.items()})
        return rows

    def test_missing_column_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad.csv"
            path.write_text("vendor_id,item_id,qty\nA,I,1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_items(path)

    def test_empty_csv_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "empty.csv"
            path.write_text("vendor_id,vendor_name,item_id,description,qty,unit_price,discount_pct,fee,deduction,tax\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_items(path)

    def test_zero_qty_rejected(self):
        rows = self._rows(qty="0")
        with self.assertRaises(ValueError):
            compute_settlement(rows, self.tiers)

    def test_negative_price_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "neg.csv"
            path.write_text(
                "vendor_id,vendor_name,item_id,description,qty,unit_price,discount_pct,fee,deduction,tax\n"
                "V1,Name,I1,Thing,1,-5,0,0,0,0\n",
                encoding="utf-8",
            )
            rows = load_items(path)
            with self.assertRaises(ValueError):
                compute_settlement(rows, self.tiers)

    def test_unknown_tier_override_rejected(self):
        raw = json.loads(TIERS.read_text(encoding="utf-8"))
        raw["tier_overrides"] = {"anything": "no-such-tier"}
        rows = self._rows()
        with self.assertRaises(ValueError):
            compute_settlement(rows, parse_config(raw)[0], raw["tier_overrides"])

    def test_vendor_tier_override(self):
        raw = json.loads(TIERS.read_text(encoding="utf-8"))
        raw["tier_overrides"] = {"V101": "booth"}
        rows = self._rows()
        s = compute_settlement(rows, parse_config(raw)[0], raw["tier_overrides"])
        p1 = next(i for i in s.items if i.item_id == "P001")
        self.assertEqual(p1.tier_name, "booth")
        self.assertEqual(p1.commission_cents, 360)   # 5% of 72.00


class OutputCsvTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows = load_items(SAMPLE_ITEMS)
        cls.tiers, _ = load_config(TIERS)
        cls.settlement = compute_settlement(rows, cls.tiers)

    def test_official_csv_contents(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "official.csv"
            write_official_csv(self.settlement, path)
            rows = _read_csv(path)
        self.assertEqual(len(rows), 9)  # 8 items + totals (DictReader skips blank line)
        total = rows[-1]
        self.assertEqual(total["vendor_id"], "TOTALS")
        self.assertEqual(total["payout"], "425.82")
        p1 = rows[0]
        self.assertEqual(p1["item_id"], "P001")
        self.assertEqual(p1["payout"], "60.70")

    def test_summary_csv_contents(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "summary.csv"
            write_summary_csv(self.settlement, path)
            rows = _read_csv(path)
        self.assertEqual(len(rows), 5)  # 4 vendors + totals
        total = rows[-1]
        self.assertEqual(total["vendor_id"], "TOTALS")
        self.assertEqual(total["payout"], "425.82")
        v202 = next(r for r in rows if r["vendor_id"] == "V202")
        self.assertEqual(v202["payout"], "115.25")


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        return cli_main(list(argv))

    def test_end_to_end_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "rep.csv"
            summ = Path(td) / "sum.csv"
            rc = self.run_cli(
                str(SAMPLE_ITEMS),
                "--config", str(TIERS),
                "-o", str(out),
                "--summary-out", str(summ),
                "--quiet",
            )
            self.assertEqual(rc, 0)
            self.assertTrue(out.exists() and summ.exists())
            rows = _read_csv(out)
            payout_total = sum(
                int(r["payout"].replace(".", "")) for r in rows if r["vendor_id"] != "TOTALS"
            )
            self.assertEqual(payout_total, 42582)

    def test_missing_file_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "nope.csv"
            rc = self.run_cli(str(bad), "--quiet")
            self.assertEqual(rc, 1)

    def test_gets_version(self):
        result = subprocess.run(
            [sys.executable, "-m", "consignment_payouts", "--version"],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("consignment-payouts", result.stdout)

    def test_smoke_cli_example_report_from_help(self):
        # also exercises argparse help path
        result = subprocess.run(
            [sys.executable, "-m", "consignment_payouts", "--help"],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--summary-out", result.stdout)


if __name__ == "__main__":
    unittest.main()
