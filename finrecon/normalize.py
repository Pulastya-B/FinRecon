#!/usr/bin/env python3
"""
Tier 0 -- normalisation.

Six CSVs written by three different systems become one canonical stream of
`LedgerEntry` objects. Nothing here matches anything: this tier's entire job is
to make the sources *comparable*, so that every tier above it argues about
evidence rather than about parsing.

Three commitments, each of which is a bug this tier refuses to pass upward.

1. Money is integer paise, converted from the CSV's decimal STRING.
   pandas infers `float64` on a money column by default -- "1234.00" becomes
   1234.0 and every comparison downstream inherits IEEE-754 error. Every read
   here is `dtype=str`, and conversion is explicit.

2. Time is a single timezone. Three sources emit three shapes -- a date, a
   naive local timestamp, a settlement date -- and comparing a naive datetime
   to an aware one raises, while comparing two naive datetimes from different
   zones silently lies. Everything lands on one aware instant.

3. Absence is `None`, never a guess. When a bank narration was clipped before
   its UTR, `references["utr"]` is None. A plausible reconstruction here would
   be indistinguishable from a real reference two tiers up, and the wrong
   match is the expensive error mode -- so this tier records that it does not
   know, and lets Tier 2/3 recover the link from amount and batch arithmetic.

The original CSV row is retained verbatim on every entry (`raw_row`), because
a reconciliation decision that cannot be traced back to the bytes it was made
from is not auditable.

Run:
    python -m finrecon.normalize --data data/seed42
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterator, Mapping

import pandas as pd

from .money import rupees_to_paise

# IST as a fixed offset rather than ZoneInfo("Asia/Kolkata"): India has never
# observed DST, so the offset is exact, and a fixed offset needs no tzdata
# package -- which is absent from a default Windows install and from
# requirements.txt. A normaliser that raises on a missing tz database makes
# parsing depend on the host, which is the same class of problem as float money.
IST = timezone(timedelta(hours=5, minutes=30), "IST")

# The six emitted ledgers, and the ONLY files this module may open.
# ground_truth.json is deliberately absent, and the allowlist is enforced at
# read time: the matcher must never see the oracle, because precision measured
# by a pipeline that read the answer key measures nothing.
SOURCE_FILES: dict[str, str] = {
    "orders": "orders.csv",
    "payments": "gateway_payments.csv",
    "refunds": "gateway_refunds.csv",
    "chargebacks": "gateway_chargebacks.csv",
    "settlements": "gateway_settlements.csv",
    "bank": "bank.csv",
}

# A UTR is exactly four uppercase letters then twelve digits. The lookarounds
# stop the pattern claiming a UTR out of the tail of a longer alphanumeric run
# -- a partial reference that parses is worse than none, because it looks
# authoritative.
UTR_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{4}\d{12})(?![0-9])")

# Every entry carries every reference key, set to None where the source has no
# such field. Uniform shape means a tier above can ask `e.references["utr"]` of
# any entry without first asking what kind of entry it is holding.
REFERENCE_KEYS: tuple[str, ...] = (
    "order_id",
    "customer_id",
    "payment_id",
    "refund_id",
    "chargeback_id",
    "settlement_id",
    "utr",
)


# --------------------------------------------------------------------------
# Canonical entry
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class LedgerEntry:
    """One row from any of the six sources, in canonical form.

    `amount_paise` is the row's headline amount as the source states it. It is
    NOT re-signed by direction: a refund's amount is positive here, exactly as
    the gateway wrote it, because the settlement equation applies its own signs
    and a value already negated once gets negated twice. Direction lives in
    `entry_type`; `signed_amount_paise` exposes it for callers summing a mixed
    stream.

    The bank is the one source with two money columns rather than one; there
    `amount_paise` is credit - debit, the only faithful way to render a
    two-column row as a single number.
    """

    entry_id: str
    source: str                      # orders | payments | ... | bank
    entry_type: str                  # order | payment | refund | ... | bank_credit
    timestamp: datetime              # tz-aware, always IST
    amount_paise: int
    amounts: Mapping[str, int]       # every money column on the row, in paise
    references: Mapping[str, str | None]
    text: str                        # free-text narration; "" where none exists
    raw_row: Mapping[str, str]       # the original CSV row, verbatim strings

    # Date-only sources (orders, settlements, bank) carry no clock. They sit at
    # IST midnight, and this property hands back the calendar day so day-level
    # comparisons never accidentally compare against a real time-of-day.
    @property
    def event_date(self) -> date:
        return self.timestamp.date()

    @property
    def signed_amount_paise(self) -> int:
        """Cash-flow impact on the merchant, for callers summing a mixed stream."""
        if self.entry_type in ("refund", "chargeback"):
            return -self.amount_paise
        return self.amount_paise

    @property
    def has_utr(self) -> bool:
        return self.references.get("utr") is not None


@dataclass
class NormalizedLedgers:
    """The six normalised streams, kept separate.

    Not concatenated into one list by default: the sources have genuinely
    different authority -- the gateway's foreign keys are reliable, the bank's
    narration is not -- and flattening them invites a tier above to forget
    which is which.
    """

    orders: list[LedgerEntry] = field(default_factory=list)
    payments: list[LedgerEntry] = field(default_factory=list)
    refunds: list[LedgerEntry] = field(default_factory=list)
    chargebacks: list[LedgerEntry] = field(default_factory=list)
    settlements: list[LedgerEntry] = field(default_factory=list)
    bank: list[LedgerEntry] = field(default_factory=list)
    source_dir: Path | None = None

    def counts(self) -> dict[str, int]:
        return {name: len(getattr(self, name)) for name in SOURCE_FILES}

    def all_entries(self) -> Iterator[LedgerEntry]:
        for name in SOURCE_FILES:
            yield from getattr(self, name)

    def __len__(self) -> int:
        return sum(self.counts().values())

    def by_id(self) -> dict[str, LedgerEntry]:
        """Flat entry-id index. Safe because ids are unique across sources by
        construction -- ord_ / pay_ / rfnd_ / cb_ / setl_ / bank_ prefixes."""
        return {e.entry_id: e for e in self.all_entries()}


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------
def extract_utr(narration: str) -> str | None:
    """Pull a UTR out of free-text narration, or return None.

    None is a real answer, not a failure. `narration_truncated` clips the
    descriptor at 35 characters and the UTR is frequently the casualty; the
    money is still real and the settlement still resolvable, but the
    machine-readable handle is gone. Guessing one -- taking a prefix, taking
    the nearest reference-shaped token, reconstructing from the date -- would
    manufacture evidence, and a wrong match is silent, plausible, and lands in
    the books. Recovery belongs to Tier 2/3, from amount and batch arithmetic.
    """
    m = UTR_RE.search(narration or "")
    return m.group(1) if m else None


def _paise(value: str) -> int:
    """CSV decimal string -> integer paise. No float hop, ever."""
    return rupees_to_paise(value)


def _parse_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


def _to_ist(value: str) -> datetime:
    """Parse a source timestamp onto the single project timezone.

    The gateway writes naive ISO local timestamps; the other sources write bare
    dates. Both are IST, and making that explicit here is what stops a naive/
    aware comparison raising -- or worse, two naive values from notionally
    different zones comparing successfully and wrongly.
    """
    dt = datetime.fromisoformat(value.strip())
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def _date_to_ist(d: date) -> datetime:
    """Date-only source -> IST midnight, so every entry is one comparable type."""
    return datetime.combine(d, time.min, tzinfo=IST)


def _refs(**kwargs: str | None) -> dict[str, str | None]:
    """Build a full reference dict, defaulting every unstated key to None."""
    refs: dict[str, str | None] = {k: None for k in REFERENCE_KEYS}
    for key, value in kwargs.items():
        if key not in refs:
            raise KeyError(f"unknown reference key {key!r}")
        # "" is how the generator writes an absent FK; that is absence, not a
        # value, and it must not survive as a falsy string into a join key.
        refs[key] = value if value else None
    return refs


def _read_csv(data_dir: Path, filename: str) -> pd.DataFrame:
    """Read one source file as pure strings.

    dtype=str on EVERY column, not just the money ones: a superset of the
    project rule that also removes the chance a column added later is silently
    inferred. keep_default_na=False keeps an empty foreign key as "" rather
    than NaN, so absence stays a string decision instead of a float one.
    """
    if filename not in SOURCE_FILES.values():
        # Executable form of the ground-truth firewall -- see SOURCE_FILES.
        raise ValueError(f"normalize.py may not read {filename!r}")
    path = data_dir / filename
    if not path.exists():
        raise FileNotFoundError(f"missing source file: {path}")
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def _rows(df: pd.DataFrame) -> Iterator[dict[str, str]]:
    for row in df.to_dict("records"):
        yield {k: ("" if v is None else str(v)) for k, v in row.items()}


# --------------------------------------------------------------------------
# Per-source normalisers
# --------------------------------------------------------------------------
def _normalize_orders(df: pd.DataFrame) -> list[LedgerEntry]:
    out = []
    for r in _rows(df):
        amount = _paise(r["amount"])
        out.append(
            LedgerEntry(
                entry_id=r["order_id"],
                source="orders",
                entry_type="order",
                timestamp=_date_to_ist(_parse_date(r["order_date"])),
                amount_paise=amount,
                amounts={"amount_paise": amount},
                # status rides in raw_row rather than references: 'cancelled'
                # is a fact about the order, not a link to another ledger.
                references=_refs(order_id=r["order_id"], customer_id=r["customer_id"]),
                text="",
                raw_row=r,
            )
        )
    return out


def _normalize_payments(df: pd.DataFrame) -> list[LedgerEntry]:
    out = []
    for r in _rows(df):
        amount = _paise(r["amount"])
        out.append(
            LedgerEntry(
                entry_id=r["payment_id"],
                source="payments",
                entry_type="payment",
                timestamp=_to_ist(r["captured_at"]),
                amount_paise=amount,
                # Fee and tax travel with the gross because Tier 3 runs the
                # settlement equation backwards and needs every term of it.
                amounts={
                    "amount_paise": amount,
                    "fee_paise": _paise(r["fee"]),
                    "tax_on_fee_paise": _paise(r["tax_on_fee"]),
                },
                references=_refs(
                    payment_id=r["payment_id"],
                    order_id=r["order_id"],
                    settlement_id=r["settlement_id"],
                ),
                text="",
                raw_row=r,
            )
        )
    return out


def _normalize_refunds(df: pd.DataFrame) -> list[LedgerEntry]:
    out = []
    for r in _rows(df):
        amount = _paise(r["amount"])
        out.append(
            LedgerEntry(
                entry_id=r["refund_id"],
                source="refunds",
                entry_type="refund",
                timestamp=_to_ist(r["created_at"]),
                amount_paise=amount,
                amounts={"amount_paise": amount},
                # settlement_id is the cycle that ABSORBED the deduction, not
                # the cycle the original capture settled in. Carried through
                # unmodified: cross-period attribution is Tier 3's problem, and
                # rewriting it here would destroy the evidence for it.
                references=_refs(
                    refund_id=r["refund_id"],
                    payment_id=r["payment_id"],
                    settlement_id=r["settlement_id"],
                ),
                text="",
                raw_row=r,
            )
        )
    return out


def _normalize_chargebacks(df: pd.DataFrame) -> list[LedgerEntry]:
    out = []
    for r in _rows(df):
        amount = _paise(r["amount"])
        out.append(
            LedgerEntry(
                entry_id=r["chargeback_id"],
                source="chargebacks",
                entry_type="chargeback",
                timestamp=_to_ist(r["created_at"]),
                amount_paise=amount,
                amounts={"amount_paise": amount},
                references=_refs(
                    chargeback_id=r["chargeback_id"],
                    payment_id=r["payment_id"],
                    settlement_id=r["settlement_id"],
                ),
                text="",
                raw_row=r,
            )
        )
    return out


def _normalize_settlements(df: pd.DataFrame) -> list[LedgerEntry]:
    out = []
    for r in _rows(df):
        net = _paise(r["net"])
        out.append(
            LedgerEntry(
                entry_id=r["settlement_id"],
                source="settlements",
                entry_type="settlement",
                # timestamp is settled_on, the date the payout leaves the
                # gateway, because that is the date a bank credit is compared
                # against. capture_date stays available in raw_row.
                timestamp=_date_to_ist(_parse_date(r["settled_on"])),
                # The headline amount is NET, not gross: net is what crosses
                # the bank boundary, so net is what a bank row can equal.
                amount_paise=net,
                amounts={
                    "amount_paise": net,
                    "net_paise": net,
                    "gross_paise": _paise(r["gross"]),
                    "fee_paise": _paise(r["fee"]),
                    "tax_paise": _paise(r["tax"]),
                    "refund_paise": _paise(r["refunds"]),
                    "chargeback_paise": _paise(r["chargebacks"]),
                    "withholding_paise": _paise(r["withholding"]),
                    "carry_forward_in_paise": _paise(r["carry_forward_in"]),
                    "carry_forward_out_paise": _paise(r["carry_forward_out"]),
                },
                # The settlement report carries the UTR in a typed column --
                # the one place the reference is not embedded in prose, and the
                # whole reason a bank row can be tied back at all.
                references=_refs(settlement_id=r["settlement_id"], utr=r["utr"]),
                text="",
                raw_row=r,
            )
        )
    return out


def _normalize_bank(df: pd.DataFrame) -> list[LedgerEntry]:
    out = []
    for r in _rows(df):
        credit, debit = _paise(r["credit"]), _paise(r["debit"])
        narration = r["narration"]
        out.append(
            LedgerEntry(
                entry_id=r["bank_row_id"],
                source="bank",
                # Split by direction once, here: a debit can never be a
                # settlement credit, and the tiers above should not re-derive
                # that from two columns on every pass.
                entry_type="bank_credit" if credit > 0 else "bank_debit",
                timestamp=_date_to_ist(_parse_date(r["txn_date"])),
                amount_paise=credit - debit,
                amounts={
                    "amount_paise": credit - debit,
                    "credit_paise": credit,
                    "debit_paise": debit,
                    "balance_paise": _paise(r["balance"]),
                },
                # utr is the ONLY reference a bank row can carry, and it is
                # frequently None. No order_id, no payment_id, no composition:
                # this is the boundary the linkage dies at.
                references=_refs(utr=extract_utr(narration)),
                text=narration,
                raw_row=r,
            )
        )
    return out


_NORMALIZERS = {
    "orders": _normalize_orders,
    "payments": _normalize_payments,
    "refunds": _normalize_refunds,
    "chargebacks": _normalize_chargebacks,
    "settlements": _normalize_settlements,
    "bank": _normalize_bank,
}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def load(data_dir: str | Path) -> NormalizedLedgers:
    """Load and normalise all six ledgers from a data directory."""
    path = Path(data_dir)
    if not path.is_dir():
        raise NotADirectoryError(f"no such data directory: {path}")

    ledgers = NormalizedLedgers(source_dir=path)
    for name, filename in SOURCE_FILES.items():
        setattr(ledgers, name, _NORMALIZERS[name](_read_csv(path, filename)))
    return ledgers


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tier 0 -- normalise the six ledgers.")
    ap.add_argument("--data", default="data/seed42", help="dataset directory")
    args = ap.parse_args(argv)

    ledgers = load(args.data)
    counts = ledgers.counts()

    print(f"\nnormalised {args.data}")
    print("-" * 48)
    for name, filename in SOURCE_FILES.items():
        print(f"  {name:<13} {counts[name]:>6}   {filename}")
    print("-" * 48)
    print(f"  {'TOTAL':<13} {len(ledgers):>6}")

    credits = [e for e in ledgers.bank if e.entry_type == "bank_credit"]
    with_utr = [e for e in credits if e.has_utr]
    print(
        f"\n  bank credits {len(credits)}, UTR recovered {len(with_utr)}, "
        f"UTR absent {len(credits) - len(with_utr)}"
    )
    print("  absent UTRs stay None -- Tier 2/3 recovers those from arithmetic.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
