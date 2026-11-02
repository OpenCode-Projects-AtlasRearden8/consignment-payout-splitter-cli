"""Payout engine.

Money math uses decimal.Decimal stored as integer cents, with
ROUND_HALF_UP applied at each rounding point. Rounding points and
order are fixed so results are fully deterministic:

1. gross             = qty * unit_price_cents                     (exact)
2. discount_cents    = HALF_UP(gross * discount_pct / 100)
3. net_sale_cents    = gross - discount_cents                     (exact)
4. commission_cents  = HALF_UP(net_sale_cents * tier_rate / 100)
5. payout_cents      = net_sale_cents - commission_cents + fee_cents - deduction_cents (exact)

Sales tax is recorded pass-through only (collected on behalf of the
taxing authority); it never affects the vendor payout. Per-vendor
summary rows sum exact integer cents, so no additional rounding error
can accumulate at the summary level.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

CENT = Decimal("1")


def _to_cents(value: str) -> int:
    """Parse a decimal string (e.g. '19.99') into integer cents."""
    d = Decimal(value.strip())
    return int(d * 100)


def _cents_to_str(cents: int) -> str:
    return f"{Decimal(cents) / 100:.2f}"


def _round(dec: Decimal) -> int:
    """Round a Decimal (in cents) HALF_UP to an integer number of cents."""
    return int(dec.quantize(CENT, rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class CommissionTier:
    name: str
    rate_pct: Decimal
    min_net_sale: Decimal  # exclusive lower bound; 0 = base tier
    max_net_sale: Decimal | None  # inclusive upper bound; None = no cap

    def applies_to(self, net_sale: Decimal) -> bool:
        if net_sale < self.min_net_sale:
            return False
        return self.max_net_sale is None or net_sale <= self.max_net_sale


@dataclass
class ItemLine:
    vendor_id: str
    vendor_name: str
    item_id: str
    description: str
    qty: int
    unit_price_cents: int
    discount_pct: Decimal
    fee_cents: int
    deduction_cents: int
    tax_cents: int
    tier_name: str
    gross_cents: int
    discount_cents: int
    net_sale_cents: int
    commission_cents: int
    payout_cents: int


@dataclass
class VendorSummary:
    vendor_id: str
    vendor_name: str
    item_count: int
    total_qty: int
    gross_cents: int
    discount_cents: int
    net_sale_cents: int
    commission_cents: int
    fee_cents: int
    deduction_cents: int
    tax_cents: int
    payout_cents: int


@dataclass
class Settlement:
    items: list[ItemLine] = field(default_factory=list)
    vendors: list[VendorSummary] = field(default_factory=list)
    grand_payout_cents: int = 0
    grand_net_sale_cents: int = 0
    grand_commission_cents: int = 0
    grand_gross_cents: int = 0
    grand_tax_cents: int = 0
    grand_fee_cents: int = 0
    grand_deduction_cents: int = 0


def parse_config(raw: dict) -> tuple[list[CommissionTier], Decimal]:
    """Parse a tiers config dict. Returns (tiers sorted by min_net_sale, default_rate)."""
    tiers = []
    for t in raw.get("tiers", []):
        tiers.append(
            CommissionTier(
                name=str(t["name"]),
                rate_pct=Decimal(str(t["rate_pct"])),
                min_net_sale=Decimal(str(t.get("min_net_sale", "0"))),
                max_net_sale=(
                    Decimal(str(t["max_net_sale"])) if t.get("max_net_sale") is not None else None
                ),
            )
        )
    if not tiers:
        raise ValueError("config must contain at least one tier")

    tiers.sort(key=lambda t: t.min_net_sale)

    # Sanity check: ranges must be sorted and must not overlap.
    prev_max: Decimal | None = None
    for t in tiers:
        if prev_max is not None and t.min_net_sale <= prev_max:
            raise ValueError(f"tier '{t.name}' overlaps or adjoins the previous tier's range")
        prev_max = t.max_net_sale

    # An uncapped (max is None) tier earlier in the list swallows every later tier.
    for t in tiers[:-1]:
        if t.max_net_sale is None:
            raise ValueError(f"tier '{t.name}' is uncapped but is not the last tier")

    default_rate = Decimal(str(raw.get("default_rate_pct", tiers[0].rate_pct)))
    return tiers, default_rate


def load_config(path: str | Path) -> tuple[list[CommissionTier], Decimal]:
    """Load a tiers JSON file."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    try:
        return parse_config(raw)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def _pick_tier(
    item_id: str,
    vendor_id: str,
    net_sale: Decimal,
    tiers: list[CommissionTier],
    tier_overrides: dict[str, str],
) -> CommissionTier:
    override = tier_overrides.get(item_id) or tier_overrides.get(vendor_id)
    if override:
        for t in tiers:
            if t.name == override:
                return t
        raise ValueError(f"tier override '{override}' for '{item_id}' not found in config")
    for t in tiers:
        if t.applies_to(net_sale):
            return t
    raise ValueError(f"no tier covers net sale {net_sale} for item '{item_id}'")


def load_items(path: str | Path) -> list[dict]:
    """Load item rows from a CSV, validating required columns."""
    required = {
        "vendor_id",
        "vendor_name",
        "item_id",
        "description",
        "qty",
        "unit_price",
        "discount_pct",
        "fee",
        "deduction",
        "tax",
    }
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: empty CSV")
        missing = required - set(reader.fieldnames)
        if missing:
            raise ValueError(f"{path}: missing columns: {', '.join(sorted(missing))}")
        rows = list(reader)
    if not rows:
        raise ValueError(f"{path}: no item rows")
    return rows


def compute_settlement(
    rows: list[dict],
    tiers: list[CommissionTier],
    tier_overrides: dict[str, str] | None = None,
) -> Settlement:
    """Compute per-item payouts and per-vendor summaries from validated CSV rows."""
    overrides = tier_overrides or {}
    tier_names = {t.name for t in tiers}
    for key, name in overrides.items():
        if not isinstance(name, str) or name not in tier_names:
            raise ValueError(f"tier override references unknown tier '{name}' (key '{key}')")
    items: list[ItemLine] = []
    seen_combine = set()

    for row in rows:
        vendor_id = row["vendor_id"].strip()
        item_id = row["item_id"].strip()
        if not vendor_id:
            raise ValueError(f"row for item '{item_id or '?'}': vendor_id is empty")
        if not item_id:
            raise ValueError(f"row for vendor '{vendor_id}': item_id is empty")
        key = (vendor_id, item_id)
        if key in seen_combine:
            raise ValueError(
                f"duplicate vendored item found: vendor '{vendor_id}' item '{item_id}'. "
                "Combine them into a single row using higher qty, or give items "
                "distinct item_ids."
            )
        seen_combine.add(key)

        qty = int(row["qty"].strip())

        if qty <= 0:
            raise ValueError(f"item '{item_id}': qty must be positive")

        unit_price_cents = _to_cents(row["unit_price"])
        if unit_price_cents < 0:
            raise ValueError(f"item '{item_id}': negative unit_price")

        discount_pct = Decimal(row["discount_pct"].strip())
        gross = qty * unit_price_cents
        discount_cents = _round(gross * discount_pct / Decimal(100))
        net_sale = gross - discount_cents

        tier = _pick_tier(item_id, vendor_id, Decimal(net_sale) / 100, tiers, overrides)
        commission_cents = _round(Decimal(net_sale) * tier.rate_pct / Decimal(100))

        fee_cents = _to_cents(row["fee"]) if row["fee"].strip() else 0
        deduction_cents = _to_cents(row["deduction"]) if row["deduction"].strip() else 0
        tax_cents = _to_cents(row["tax"]) if row["tax"].strip() else 0

        payout = net_sale - commission_cents - fee_cents - deduction_cents

        items.append(
            ItemLine(
                vendor_id=vendor_id,
                vendor_name=row["vendor_name"].strip(),
                item_id=item_id,
                description=row["description"].strip(),
                qty=qty,
                unit_price_cents=unit_price_cents,
                discount_pct=discount_pct,
                fee_cents=fee_cents,
                deduction_cents=deduction_cents,
                tax_cents=tax_cents,
                tier_name=tier.name,
                gross_cents=gross,
                discount_cents=discount_cents,
                net_sale_cents=net_sale,
                commission_cents=commission_cents,
                payout_cents=payout,
            )
        )

    # Any override key must match a real vendor_id or item_id present in this batch.
    known_ids = {r["vendor_id"].strip() for r in rows} | {r["item_id"].strip() for r in rows}
    for key in overrides:
        if key not in known_ids:
            raise ValueError(f"tier override key '{key}' matches no vendor_id or item_id")

    vendors: dict[str, VendorSummary] = {}
    for it in items:
        vs = vendors.setdefault(
            it.vendor_id,
            VendorSummary(
                vendor_id=it.vendor_id,
                vendor_name=it.vendor_name,
                item_count=0,
                total_qty=0,
                gross_cents=0,
                discount_cents=0,
                net_sale_cents=0,
                commission_cents=0,
                fee_cents=0,
                deduction_cents=0,
                tax_cents=0,
                payout_cents=0,
            ),
        )
        vs.item_count += 1
        vs.total_qty += it.qty
        vs.gross_cents += it.gross_cents
        vs.discount_cents += it.discount_cents
        vs.net_sale_cents += it.net_sale_cents
        vs.commission_cents += it.commission_cents
        vs.fee_cents += it.fee_cents
        vs.deduction_cents += it.deduction_cents
        vs.tax_cents += it.tax_cents
        vs.payout_cents += it.payout_cents

    s = Settlement(
        items=items,
        vendors=list(vendors.values()),
    )
    s.grand_payout_cents = sum(v.payout_cents for v in s.vendors)
    s.grand_net_sale_cents = sum(v.net_sale_cents for v in s.vendors)
    s.grand_commission_cents = sum(v.commission_cents for v in s.vendors)
    s.grand_gross_cents = sum(v.gross_cents for v in s.vendors)
    s.grand_tax_cents = sum(v.tax_cents for v in s.vendors)
    s.grand_fee_cents = sum(v.fee_cents for v in s.vendors)
    s.grand_deduction_cents = sum(v.deduction_cents for v in s.vendors)
    return s


OFFICIAL_COLUMNS = [
    "vendor_id",
    "vendor_name",
    "item_id",
    "qty",
    "description",
    "gross",
    "discount",
    "net_sale",
    "commission",
    "commission_tier",
    "deduction",
    "fee",
    "tax",
    "payout",
]

ITEM_TITLES = [
    ("vendor_id", lambda it: it.vendor_id),
    ("vendor_name", lambda it: it.vendor_name),
    ("item_id", lambda it: it.item_id),
    ("qty", lambda it: str(it.qty)),
    ("description", lambda it: it.description),
    ("gross", lambda it: _cents_to_str(it.gross_cents)),
    ("discount", lambda it: _cents_to_str(it.discount_cents)),
    ("net_sale", lambda it: _cents_to_str(it.net_sale_cents)),
    ("commission", lambda it: _cents_to_str(it.commission_cents)),
    ("commission_tier", lambda it: it.tier_name),
    ("deduction", lambda it: _cents_to_str(it.deduction_cents)),
    ("fee", lambda it: _cents_to_str(it.fee_cents)),
    ("tax", lambda it: _cents_to_str(it.tax_cents)),
    ("payout", lambda it: _cents_to_str(it.payout_cents)),
]

SUMMARY_COLUMNS = [
    "vendor_id",
    "vendor_name",
    "items",
    "qty_sold",
    "gross",
    "discount",
    "net_sale",
    "commission",
    "deduction",
    "fee",
    "tax_collected",
    "payout",
]


def write_official_csv(settlement: Settlement, path: str | Path) -> None:
    """Write the per-item settlement to CSV (the 'official report')."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(OFFICIAL_COLUMNS)
        for item in settlement.items:
            w.writerow([fn(item) for _, fn in ITEM_TITLES])
        totals = settlement
        w.writerow([])
        w.writerow(
            [
                "TOTALS",
                "",
                "",
                "",
                "",
                _cents_to_str(totals.grand_gross_cents),
                "",
                _cents_to_str(totals.grand_net_sale_cents),
                _cents_to_str(totals.grand_commission_cents),
                "",
                _cents_to_str(totals.grand_deduction_cents),
                _cents_to_str(totals.grand_fee_cents),
                _cents_to_str(totals.grand_tax_cents),
                _cents_to_str(totals.grand_payout_cents),
            ]
        )


def write_summary_csv(settlement: Settlement, path: str | Path) -> None:
    """Write the per-vendor payout summary to CSV."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(SUMMARY_COLUMNS)
        for vs in settlement.vendors:
            w.writerow(
                [
                    vs.vendor_id,
                    vs.vendor_name,
                    vs.item_count,
                    vs.total_qty,
                    _cents_to_str(vs.gross_cents),
                    _cents_to_str(vs.discount_cents),
                    _cents_to_str(vs.net_sale_cents),
                    _cents_to_str(vs.commission_cents),
                    _cents_to_str(vs.deduction_cents),
                    _cents_to_str(vs.fee_cents),
                    _cents_to_str(vs.tax_cents),
                    _cents_to_str(vs.payout_cents),
                ]
            )
        w.writerow(
            [
                "TOTALS",
                "",
                sum(v.item_count for v in settlement.vendors),
                sum(v.total_qty for v in settlement.vendors),
                _cents_to_str(settlement.grand_gross_cents),
                "",
                _cents_to_str(settlement.grand_net_sale_cents),
                _cents_to_str(settlement.grand_commission_cents),
                _cents_to_str(settlement.grand_deduction_cents),
                _cents_to_str(settlement.grand_fee_cents),
                _cents_to_str(settlement.grand_tax_cents),
                _cents_to_str(settlement.grand_payout_cents),
            ]
        )


def format_report(settlement: Settlement) -> str:
    """Human-readable report for stdout."""
    lines: list[str] = []
    lines.append("CONSIGNMENT SETTLEMENT REPORT")
    lines.append("=" * 78)
    for vs in settlement.vendors:
        lines.append("")
        lines.append(f"Vendor: {vs.vendor_name} (id {vs.vendor_id})")
        lines.append(f"  items {vs.item_count}   qty {vs.total_qty}")
        lines.append(f"  gross       {_cents_to_str(vs.gross_cents):>12}")
        lines.append(f"  net_sale    {_cents_to_str(vs.net_sale_cents):>12}")
        lines.append(f"  commission  {_cents_to_str(vs.commission_cents):>12}")
        lines.append(f"  fees+deduc  {_cents_to_str(vs.fee_cents + vs.deduction_cents):>12}")
        lines.append(f"  tax (info)  {_cents_to_str(vs.tax_cents):>12}")
        lines.append(f"  PAYOUT      {_cents_to_str(vs.payout_cents):>12}")
        lines.append("  ---")
        for it in settlement.items:
            if it.vendor_id != vs.vendor_id:
                continue
            lines.append(
                f"    {it.item_id:<10} x{it.qty:<3} {it.description[:24]:<24} "
                f"tier={it.tier_name:<8} payout {_cents_to_str(it.payout_cents):>10}"
            )
    lines.append("")
    lines.append("=" * 78)
    lines.append(f"TOTAL payout     {_cents_to_str(settlement.grand_payout_cents):>12}")
    lines.append(f"TOTAL net_sale   {_cents_to_str(settlement.grand_net_sale_cents):>12}")
    lines.append(f"TOTAL commission {_cents_to_str(settlement.grand_commission_cents):>12}")
    lines.append(f"TOTAL tax (info) {_cents_to_str(settlement.grand_tax_cents):>12}")
    return "\n".join(lines)
