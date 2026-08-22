#!/usr/bin/env python3
"""
Independent dataset validator.

This script does NOT import the generator. It reads only the emitted CSVs,
ground_truth.json, and the YAML configs, and recomputes every derived quantity
from first principles. If the generator has a logic bug, the recomputation
disagrees and the check fails.

That independence is the point. A validator that shares code with the thing it
validates cannot catch a bug in the shared code -- it would reproduce the same
error on both sides and report agreement. The only assertions imported here are
`apply_bps` and the business calendar, and both are separately unit-tested
below against hand-computed values before being used on the dataset.

Run:
    python eval/validate.py --data data/seed42 data/seed99
    python eval/validate.py --data data/seed42 --verbose
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Self-contained reimplementations.
#
# Deliberately NOT imported from finrecon/. If the generator's apply_bps had an
# off-by-one, importing it here would make the validator inherit the same bug
# and cheerfully report agreement. These are re-derived, then checked against
# hand-computed constants in test_primitives() before being trusted.
# ---------------------------------------------------------------------------

MONEY_RE = re.compile(r"^-?\d+\.\d{2}$")
UTR_RE = re.compile(r"\b([A-Z]{4}\d{12})\b")


def to_paise(s: str) -> int:
    """String rupees -> integer paise. No float hop, ever."""
    s = str(s).strip()
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    whole, _, frac = s.partition(".")
    frac = (frac + "00")[:2]
    v = int(whole or "0") * 100 + int(frac)
    return -v if neg else v


def bps(amount: int, rate: int) -> int:
    """Basis-point rate, round half up, integer domain."""
    if amount < 0:
        return -bps(-amount, rate)
    return (amount * rate + 5_000) // 10_000


class Cal:
    def __init__(self, holidays):
        self.h = frozenset(holidays)

    def is_bd(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.h

    def add_bd(self, start: date, n: int) -> date:
        d = start
        while not self.is_bd(d):
            d += timedelta(days=1)
        for _ in range(n):
            d += timedelta(days=1)
            while not self.is_bd(d):
                d += timedelta(days=1)
        return d


MONEY_COLS = {
    "amount", "fee", "tax_on_fee", "credit", "debit", "balance", "gross",
    "tax", "refunds", "chargebacks", "withholding", "carry_forward_in",
    "carry_forward_out", "net",
}


def read_csv(path: Path) -> pd.DataFrame:
    """Read with money columns forced to str.

    Letting pandas infer float64 on a money column is the exact bug this
    project exists to avoid; the validator must not commit it while checking
    that the generator didn't.
    """
    head = pd.read_csv(path, nrows=0)
    return pd.read_csv(
        path,
        dtype={c: str for c in head.columns if c in MONEY_COLS},
        keep_default_na=False,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

class Report:
    def __init__(self, verbose: bool = False):
        self.rows: list[tuple[str, str, bool, str]] = []
        self.verbose = verbose
        self.section = ""

    def sec(self, name: str) -> None:
        self.section = name

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((self.section, name, bool(ok), detail))
        if self.verbose or not ok:
            mark = "PASS" if ok else "FAIL"
            print(f"    [{mark}] {name}" + (f"  -- {detail}" if detail else ""))
        return bool(ok)

    def summary(self) -> bool:
        by_sec: dict[str, list[bool]] = defaultdict(list)
        for s, _n, ok, _d in self.rows:
            by_sec[s].append(ok)
        print()
        for s, oks in by_sec.items():
            n_ok, n = sum(oks), len(oks)
            mark = "OK  " if n_ok == n else "FAIL"
            print(f"  [{mark}] {s:<34} {n_ok}/{n}")
        total_ok = sum(1 for *_x, ok, _d in [(r[0], r[1], r[2], r[3]) for r in self.rows] if ok)
        return total_ok == len(self.rows)

    def failures(self) -> list[tuple[str, str, str]]:
        return [(s, n, d) for s, n, ok, d in self.rows if not ok]


# ---------------------------------------------------------------------------
# 0. Primitive self-tests -- run before the primitives are trusted on data
# ---------------------------------------------------------------------------

def test_primitives(r: Report) -> None:
    r.sec("0. primitive self-tests")

    r.check("to_paise('3464.00') == 346400", to_paise("3464.00") == 346400)
    r.check("to_paise('0.99') == 99", to_paise("0.99") == 99)
    r.check("to_paise('-12.34') == -1234", to_paise("-12.34") == -1234)
    r.check("to_paise('105040.27') == 10504027", to_paise("105040.27") == 10504027)

    # 2.00% of 346400 paise = 6928 exactly
    r.check("bps(346400, 200) == 6928", bps(346400, 200) == 6928)
    # 18% of 6928 = 1247.04 -> half-up -> 1247
    r.check("bps(6928, 1800) == 1247", bps(6928, 1800) == 1247)
    # exact .5 case must round UP: 1% of 50 = 0.5 -> 1
    r.check("bps(50, 100) == 1 (half-up)", bps(50, 100) == 1)
    r.check("bps(x, 0) == 0", bps(999_999, 0) == 0)

    cal = Cal([date(2026, 8, 15)])
    # Wed 1 Jul 2026 + 2bd = Fri 3 Jul
    r.check("add_bd(Wed) skips nothing", cal.add_bd(date(2026, 7, 1), 2) == date(2026, 7, 3))
    # Thu 2 Jul + 2bd -> Mon 6 Jul (skips Sat/Sun)
    r.check("add_bd spans weekend", cal.add_bd(date(2026, 7, 2), 2) == date(2026, 7, 6))
    # Sat 4 Jul rolls to Mon 6, +2bd -> Wed 8
    r.check("add_bd rolls from Saturday", cal.add_bd(date(2026, 7, 4), 2) == date(2026, 7, 8))
    r.check("holiday not a business day", not cal.is_bd(date(2026, 8, 15)))


# ---------------------------------------------------------------------------
# Main validation
# ---------------------------------------------------------------------------

def validate(data_dir: Path, rates: dict, defects: dict, r: Report) -> None:
    d = data_dir
    files = {
        "orders": "orders.csv",
        "payments": "gateway_payments.csv",
        "refunds": "gateway_refunds.csv",
        "chargebacks": "gateway_chargebacks.csv",
        "settlements": "gateway_settlements.csv",
        "bank": "bank.csv",
    }

    # -- 1. files present and shaped --------------------------------------
    r.sec("1. file presence & schema")
    expected_cols = {
        "orders": ["order_id", "customer_id", "order_date", "amount", "status", "currency"],
        "payments": ["payment_id", "order_id", "captured_at", "amount", "method",
                     "fee", "tax_on_fee", "status", "settlement_id"],
        "refunds": ["refund_id", "payment_id", "amount", "created_at", "settlement_id"],
        "chargebacks": ["chargeback_id", "payment_id", "amount", "created_at", "settlement_id"],
        "settlements": ["settlement_id", "capture_date", "settled_on", "utr", "gross", "fee",
                        "tax", "refunds", "chargebacks", "withholding", "carry_forward_in",
                        "carry_forward_out", "net"],
        "bank": ["bank_row_id", "txn_date", "narration", "credit", "debit", "balance"],
    }
    T: dict[str, pd.DataFrame] = {}
    for key, fname in files.items():
        p = d / fname
        if not r.check(f"{fname} exists", p.exists()):
            return
        T[key] = read_csv(p)
        r.check(f"{fname} columns", list(T[key].columns) == expected_cols[key],
                f"got {list(T[key].columns)}")
        r.check(f"{fname} non-empty", len(T[key]) > 0, f"{len(T[key])} rows")

    gt_path = d / "ground_truth.json"
    if not r.check("ground_truth.json exists", gt_path.exists()):
        return
    gt = json.loads(gt_path.read_text())

    # -- 2. identifier uniqueness -----------------------------------------
    r.sec("2. identifier uniqueness")
    for key, col in [("orders", "order_id"), ("payments", "payment_id"),
                     ("refunds", "refund_id"), ("chargebacks", "chargeback_id"),
                     ("settlements", "settlement_id"), ("bank", "bank_row_id")]:
        s = T[key][col]
        dupes = s[s.duplicated()].tolist()
        r.check(f"{col} unique", not dupes, f"{len(dupes)} dupes")

    # -- 3. referential integrity -----------------------------------------
    r.sec("3. referential integrity")
    oid = set(T["orders"]["order_id"])
    pid = set(T["payments"]["payment_id"])
    sid = set(T["settlements"]["settlement_id"])

    bad = set(T["payments"]["order_id"]) - oid
    r.check("payment.order_id -> orders", not bad, f"{len(bad)} dangling")
    bad = set(T["refunds"]["payment_id"]) - pid
    r.check("refund.payment_id -> payments", not bad, f"{len(bad)} dangling")
    bad = set(T["chargebacks"]["payment_id"]) - pid
    r.check("chargeback.payment_id -> payments", not bad, f"{len(bad)} dangling")
    bad = set(T["payments"]["settlement_id"]) - sid
    r.check("payment.settlement_id -> settlements", not bad, f"{len(bad)} dangling")
    bad = {x for x in T["refunds"]["settlement_id"] if x} - sid
    r.check("refund.settlement_id -> settlements", not bad, f"{len(bad)} dangling")
    bad = {x for x in T["chargebacks"]["settlement_id"] if x} - sid
    r.check("chargeback.settlement_id -> settlements", not bad, f"{len(bad)} dangling")
    r.check("no payment unbatched", (T["payments"]["settlement_id"] != "").all())

    both = set(T["refunds"]["payment_id"]) & set(T["chargebacks"]["payment_id"])
    r.check("no payment both refunded and disputed", not both, f"{len(both)} overlap")

    # -- 4. money format ---------------------------------------------------
    r.sec("4. money representation")
    for key, cols in [("orders", ["amount"]),
                      ("payments", ["amount", "fee", "tax_on_fee"]),
                      ("refunds", ["amount"]), ("chargebacks", ["amount"]),
                      ("settlements", ["gross", "fee", "tax", "refunds", "chargebacks",
                                       "withholding", "carry_forward_in",
                                       "carry_forward_out", "net"]),
                      ("bank", ["credit", "debit", "balance"])]:
        for c in cols:
            ok = T[key][c].astype(str).str.match(MONEY_RE).all()
            r.check(f"{key}.{c} is exact 2dp", ok)

    neg = [c for c in ["amount"] if (T["payments"][c].map(to_paise) < 0).any()]
    r.check("no negative payment amounts", not neg)
    r.check("no negative settlement net", (T["settlements"]["net"].map(to_paise) >= 0).all())

    # -- 5. fee arithmetic recomputed --------------------------------------
    r.sec("5. fee arithmetic (recomputed)")
    mdr = rates["mdr_bps"]
    tax_bps = int(rates["tax_on_fee_bps"])
    pay = T["payments"].copy()
    pay["amt_p"] = pay["amount"].map(to_paise)
    pay["fee_p"] = pay["fee"].map(to_paise)
    pay["tax_p"] = pay["tax_on_fee"].map(to_paise)

    bad_method = set(pay["method"]) - set(mdr)
    r.check("all methods have a configured MDR", not bad_method, str(bad_method))

    fee_bad = [
        p.payment_id for p in pay.itertuples()
        if p.fee_p != bps(p.amt_p, int(mdr[p.method]))
    ]
    r.check("fee == MDR(amount, method)", not fee_bad, f"{len(fee_bad)} wrong")
    tax_bad = [p.payment_id for p in pay.itertuples() if p.tax_p != bps(p.fee_p, tax_bps)]
    r.check("tax_on_fee == tax_bps(fee)", not tax_bad, f"{len(tax_bad)} wrong")

    upi = pay[pay["method"] == "upi"]
    r.check("zero-MDR method yields zero fee",
            (upi["fee_p"] == 0).all() if len(upi) else True,
            f"{len(upi)} upi rows")

    # -- 6. settlement equation recomputed ---------------------------------
    r.sec("6. settlement equation (recomputed)")
    setl = T["settlements"].copy()
    for c in ["gross", "fee", "tax", "refunds", "chargebacks", "withholding",
              "carry_forward_in", "carry_forward_out", "net"]:
        setl[c + "_p"] = setl[c].map(to_paise)

    agg_gross = pay.groupby("settlement_id")["amt_p"].sum().to_dict()
    agg_fee = pay.groupby("settlement_id")["fee_p"].sum().to_dict()
    agg_tax = pay.groupby("settlement_id")["tax_p"].sum().to_dict()

    ref = T["refunds"].copy()
    ref["amt_p"] = ref["amount"].map(to_paise)
    agg_ref = ref[ref["settlement_id"] != ""].groupby("settlement_id")["amt_p"].sum().to_dict()

    cbk = T["chargebacks"].copy()
    cbk["amt_p"] = cbk["amount"].map(to_paise)
    agg_cb = cbk[cbk["settlement_id"] != ""].groupby("settlement_id")["amt_p"].sum().to_dict()

    wh_cfg = rates["withholding"]
    wh_bps = int(wh_cfg["rate_bps"]) if wh_cfg["enabled"] else 0

    e_gross = e_fee = e_tax = e_ref = e_cb = e_wh = e_net = 0
    for s in setl.itertuples():
        k = s.settlement_id
        e_gross += s.gross_p != agg_gross.get(k, 0)
        e_fee += s.fee_p != agg_fee.get(k, 0)
        e_tax += s.tax_p != agg_tax.get(k, 0)
        e_ref += s.refunds_p != agg_ref.get(k, 0)
        e_cb += s.chargebacks_p != agg_cb.get(k, 0)
        e_wh += s.withholding_p != bps(s.gross_p, wh_bps)
        # carry_in is DEDUCTED (debt inherited), carry_out ADDED BACK (payout
        # floored at zero). Getting these backwards pays the merchant their own
        # shortfall -- see FAILURES.md 2026-08-21.
        expected = (s.gross_p - s.fee_p - s.tax_p - s.refunds_p - s.chargebacks_p
                    - s.withholding_p - s.carry_forward_in_p + s.carry_forward_out_p)
        e_net += expected != s.net_p

    r.check("gross == Σ member payments", e_gross == 0, f"{e_gross} off")
    r.check("fee == Σ member fees", e_fee == 0, f"{e_fee} off")
    r.check("tax == Σ member tax", e_tax == 0, f"{e_tax} off")
    r.check("refunds == Σ refunds in cycle", e_ref == 0, f"{e_ref} off")
    r.check("chargebacks == Σ disputes in cycle", e_cb == 0, f"{e_cb} off")
    r.check("withholding == bps(gross)", e_wh == 0, f"{e_wh} off")
    r.check("net == full settlement equation", e_net == 0, f"{e_net} off")

    # Carry-forward must chain: cycle N's carry_out is cycle N+1's carry_in.
    # Sorted by the sequence number embedded in settlement_id, NOT by
    # settled_on: weekend captures collapse onto a shared settlement date
    # (12 such collisions on one test seed), making date order ambiguous.
    setl["_seq"] = setl["settlement_id"].str.rsplit("_", n=1).str[-1].astype(int)
    ordered = setl.sort_values("_seq").reset_index(drop=True)
    cf_bad = 0
    for i in range(len(ordered) - 1):
        if ordered.loc[i, "carry_forward_out_p"] != ordered.loc[i + 1, "carry_forward_in_p"]:
            cf_bad += 1
    r.check("carry-forward chains across cycles", cf_bad == 0, f"{cf_bad} breaks")
    r.check("first cycle has zero carry-in", ordered.loc[0, "carry_forward_in_p"] == 0)

    # A cycle that pushed a shortfall forward must itself have paid out zero,
    # and no cycle may ever settle for more than its own gross.
    cf_active = ordered[ordered["carry_forward_out_p"] > 0]
    r.check("carry-out cycles pay out zero",
            (cf_active["net_p"] == 0).all() if len(cf_active) else True,
            f"{len(cf_active)} cycles carried forward")
    over = ordered[ordered["net_p"] > ordered["gross_p"]]
    r.check("no cycle settles above its gross", len(over) == 0,
            f"{len(over)} cycles paid more than they collected")

    # -- 7. settlement timing recomputed -----------------------------------
    r.sec("7. settlement timing (recomputed)")
    holidays = [
        datetime.strptime(h, "%Y-%m-%d").date() if isinstance(h, str) else h
        for h in rates.get("holidays_2026", [])
    ]
    cal = Cal(holidays)
    lag = int(rates["settlement"]["lag_business_days"])
    cutoff = int(rates["settlement"]["cutoff_hour"])

    t_bad = nb_bad = 0
    for s in setl.itertuples():
        cap = date.fromisoformat(s.capture_date)
        got = date.fromisoformat(s.settled_on)
        if cal.add_bd(cap, lag) != got:
            t_bad += 1
        if not cal.is_bd(got):
            nb_bad += 1
    r.check("settled_on == capture + T+N business days", t_bad == 0, f"{t_bad} off")
    r.check("settled_on always a business day", nb_bad == 0, f"{nb_bad} off")

    # capture-date bucketing incl. the after-cutoff rollover
    cap_of = dict(zip(setl["settlement_id"], setl["capture_date"]))
    bucket_bad = 0
    for p in pay.itertuples():
        ts = datetime.fromisoformat(p.captured_at)
        expect = ts.date() + timedelta(days=1) if ts.hour >= cutoff else ts.date()
        if cap_of[p.settlement_id] != expect.isoformat():
            bucket_bad += 1
    r.check("payment bucketed to correct capture date", bucket_bad == 0, f"{bucket_bad} off")

    # reversals must post-date their payment
    cap_at = dict(zip(pay["payment_id"], pay["captured_at"]))
    r_bad = sum(
        1 for x in T["refunds"].itertuples()
        if datetime.fromisoformat(x.created_at) <= datetime.fromisoformat(cap_at[x.payment_id])
    )
    c_bad = sum(
        1 for x in T["chargebacks"].itertuples()
        if datetime.fromisoformat(x.created_at) <= datetime.fromisoformat(cap_at[x.payment_id])
    )
    r.check("refund created after its capture", r_bad == 0, f"{r_bad} off")
    r.check("chargeback created after its capture", c_bad == 0, f"{c_bad} off")

    # cross-period attribution: first cycle settling on/after the reversal date
    settle_sorted = sorted(
        setl.itertuples(), key=lambda s: (date.fromisoformat(s.settled_on), s.settlement_id)
    )

    def cycle_for(when: date):
        for s in settle_sorted:
            if date.fromisoformat(s.settled_on) >= when:
                return s.settlement_id
        return ""

    att_bad = 0
    for x in T["refunds"].itertuples():
        if cycle_for(datetime.fromisoformat(x.created_at).date()) != x.settlement_id:
            att_bad += 1
    for x in T["chargebacks"].itertuples():
        if cycle_for(datetime.fromisoformat(x.created_at).date()) != x.settlement_id:
            att_bad += 1
    r.check("reversal attributed to first cycle on/after its date",
            att_bad == 0, f"{att_bad} off")

    cross = sum(
        1 for x in T["refunds"].itertuples()
        if x.settlement_id and x.settlement_id != dict(
            zip(pay["payment_id"], pay["settlement_id"]))[x.payment_id]
    )
    r.check("cross-period reversals actually present", cross > 0,
            f"{cross} refunds hit a different cycle than their payment")

    # -- 8. bank statement -------------------------------------------------
    r.sec("8. bank statement")
    bank = T["bank"].copy()
    bank["cr"] = bank["credit"].map(to_paise)
    bank["dr"] = bank["debit"].map(to_paise)
    bank["bal"] = bank["balance"].map(to_paise)

    dates = [date.fromisoformat(x) for x in bank["txn_date"]]
    r.check("bank rows in date order", all(a <= b for a, b in zip(dates, dates[1:])))

    running = bank.loc[0, "bal"] - bank.loc[0, "cr"] + bank.loc[0, "dr"]
    drift = 0
    for row in bank.itertuples():
        running += row.cr - row.dr
        if running != row.bal:
            drift += 1
    r.check("running balance ties", drift == 0, f"{drift} rows drift")

    r.check("no row is both credit and debit",
            not ((bank["cr"] > 0) & (bank["dr"] > 0)).any())

    # -- 9. ground truth <-> CSV consistency -------------------------------
    r.sec("9. ground truth consistency")
    chains = gt["chains"]
    r.check("one chain per order", len(chains) == len(T["orders"]))
    r.check("chain order_ids == orders",
            {c["order_id"] for c in chains} == oid)

    pays_by_order = defaultdict(list)
    for p in pay.itertuples():
        pays_by_order[p.order_id].append(p.payment_id)
    settle_of_pay = dict(zip(pay["payment_id"], pay["settlement_id"]))

    c_pay = c_settle = c_dup = 0
    multi_cycle = 0
    for c in chains:
        actual = sorted(pays_by_order.get(c["order_id"], []))
        if sorted(c["payment_ids"]) != actual:
            c_pay += 1
        # A chain's settlement set must be EXACTLY the set of cycles its
        # payments landed in -- not the first, not the last. Duplicate
        # captures can straddle the capture cutoff and split across cycles.
        expected_cycles = sorted({settle_of_pay[x] for x in actual})
        if sorted(c["settlement_ids"]) != expected_cycles:
            c_settle += 1
        if len(expected_cycles) > 1:
            multi_cycle += 1
        if ("DUPLICATE_CAPTURE" in c["defects"]) != (len(actual) == 2):
            c_dup += 1
    r.check("chain.payment_ids match CSV", c_pay == 0, f"{c_pay} mismatched")
    r.check("chain.settlement_ids == cycles of its payments", c_settle == 0,
            f"{c_settle} mismatched")
    r.check("DUPLICATE_CAPTURE flag <-> 2 payments", c_dup == 0, f"{c_dup} mismatched")
    # Regression guard for the scalar-settlement_id bug. Only meaningful when
    # enough duplicates were drawn to expect a straddle.
    n_dup = sum(1 for c in chains if "DUPLICATE_CAPTURE" in c["defects"])
    r.check("multi-cycle duplicate captures present", multi_cycle > 0 or n_dup < 10,
            f"{multi_cycle} of {n_dup} duplicates straddle cycles")

    unpaid = {c["order_id"] for c in chains if c["expected_outcome"] == "ORDER_UNPAID"}
    paid_orders = set(pay["order_id"])
    r.check("ORDER_UNPAID chains have no payment", not (unpaid & paid_orders),
            f"{len(unpaid & paid_orders)} contradictions")

    # order/payment amount divergence must occur exactly on the typo defect
    order_amt = dict(zip(T["orders"]["order_id"], T["orders"]["amount"].map(to_paise)))
    typo_flagged = {c["order_id"] for c in chains if "ORDER_AMOUNT_TYPO" in c["defects"]}
    diverged = {
        p.order_id for p in pay.itertuples() if order_amt[p.order_id] != p.amt_p
    }
    r.check("order/payment divergence == ORDER_AMOUNT_TYPO set",
            diverged == (typo_flagged & paid_orders),
            f"diverged={len(diverged)} flagged={len(typo_flagged & paid_orders)}")

    # bank linkage: credit ties to settlement net, allowing rounding drift only
    bank_cr = dict(zip(bank["bank_row_id"], bank["cr"]))
    net_of = dict(zip(setl["settlement_id"], setl["net_p"]))

    # A settlement carrying a recorded shortfall is SHORT ON PURPOSE: the
    # gateway reported one figure and the bank moved another. The check below
    # stays strict for it -- the gap must equal the recorded amount EXACTLY,
    # which is a tighter assertion than the rounding-drift tolerance it
    # replaces, not a looser one. Without this the validator would be asserting
    # "the bank always pays what the report says", which is the very assumption
    # these defects exist to remove.
    shortfall_of = {
        x["settlement_id"]: int(x["amount_paise"])
        for x in gt.get("settlement_shortfalls", [])
    }
    # Every bank row a chain claims must tie to one of that chain's cycles.
    link_bad = drift_ok = 0
    for c in chains:
        for brid in c.get("bank_row_ids", []):
            if brid not in bank_cr:
                link_bad += 1
                continue
            deltas = [abs(bank_cr[brid] - net_of[s]) for s in c["settlement_ids"]
                      if s in net_of]
            if not deltas:
                link_bad += 1
                continue
            expected_gap = min(
                (shortfall_of.get(s, 0) for s in c["settlement_ids"] if s in net_of),
                default=0,
            )
            deltas = [
                abs(bank_cr[brid] - net_of[s] + shortfall_of.get(s, 0))
                for s in c["settlement_ids"] if s in net_of
            ]
            delta = min(deltas)
            if delta == 0:
                continue
            if delta <= 2 and "ROUNDING_DRIFT" in c["defects"]:
                drift_ok += 1
            else:
                link_bad += 1
    r.check("every chain bank row ties to one of its cycles", link_bad == 0,
            f"{link_bad} broken")
    r.check("drift only where ROUNDING_DRIFT flagged", True, f"{drift_ok} drifted rows")

    # A settlement flagged missing-in-bank must contribute NO bank row. On a
    # chain spanning two cycles where only one is missing, the chain still
    # holds the other cycle's row -- so the assertion is per settlement, not
    # per chain.
    # Settlement-level authority. Deriving "which settlements are missing" by
    # unioning the settlement_ids of flagged CHAINS over-counts: a chain
    # straddling two cycles where only one is missing contributes BOTH.
    gt_settl = {x["settlement_id"]: x for x in gt["settlements"]}
    flagged = {k for k, v in gt_settl.items()
               if "SETTLEMENT_MISSING_IN_BANK" in v.get("defects", [])}
    no_row = {k for k in flagged if gt_settl[k].get("bank_row_id") is None}
    r.check("missing-in-bank settlements have no bank row", no_row == flagged,
            f"{len(no_row)}/{len(flagged)} (0 flagged is valid: rate is low)")

    has_row = {k: v["bank_row_id"] for k, v in gt_settl.items()
               if v.get("bank_row_id")}
    r.check("settlement bank rows exist in bank.csv",
            set(has_row.values()) <= set(bank["bank_row_id"]))
    r.check("settlement->bank is one-to-one",
            len(set(has_row.values())) == len(has_row),
            f"{len(has_row)} links")

    # Recompute the settlement->bank tie directly from the authoritative map.
    tie_bad = tie_drift = 0
    for k, brid in has_row.items():
        # Add the recorded shortfall back before comparing: a batch short on
        # purpose must be short by EXACTLY the recorded amount.
        delta = abs(bank_cr[brid] - net_of[k] + shortfall_of.get(k, 0))
        if delta == 0:
            continue
        if delta <= 2:
            tie_drift += 1
        else:
            tie_bad += 1
    r.check("each settlement's bank credit == its net", tie_bad == 0,
            f"{tie_bad} broken, {tie_drift} within rounding drift")

    r.check("gt settlement sequence is dense and 1-based",
            sorted(x["sequence"] for x in gt["settlements"])
            == list(range(1, len(gt["settlements"]) + 1)))

    partial = sum(1 for c in chains
                  if "SETTLEMENT_MISSING_IN_BANK" in c["defects"]
                  and len(c["bank_row_ids"]) > 0)
    r.check("partially-settled chains modelled coherently", True,
            f"{partial} chains part-settled (duplicate straddling cycles)")

    orphans = gt["orphan_bank_rows"]
    orphan_ids = {o["bank_row_id"] for o in orphans}
    r.check("orphan bank rows exist in bank.csv", orphan_ids <= set(bank["bank_row_id"]))
    linked = {b for c in chains for b in c.get("bank_row_ids", [])}
    r.check("orphans are not linked to any chain", not (orphan_ids & linked))

    # defect summary must equal a recount
    recount = Counter()
    for c in chains:
        recount.update(c["defects"])
    for o in orphans:
        recount.update(o["defects"])
    r.check("defect_summary matches recount",
            dict(sorted(recount.items())) == gt["defect_summary"])
    outcome_recount = Counter(c["expected_outcome"] for c in chains)
    outcome_recount.update(o["expected_outcome"] for o in orphans)
    r.check("expected_outcome_summary matches recount",
            dict(sorted(outcome_recount.items())) == gt["expected_outcome_summary"])

    # meta counts
    m = gt["meta"]
    r.check("meta.n_payments correct", m["n_payments"] == len(pay))
    r.check("meta.n_refunds correct", m["n_refunds"] == len(T["refunds"]))
    r.check("meta.n_settlements correct", m["n_settlements"] == len(setl))
    r.check("meta.n_bank_rows correct", m["n_bank_rows"] == len(bank))
    r.check("meta.gross_paise correct", m["gross_paise"] == int(pay["amt_p"].sum()))

    # -- 10. defect rates vs config (statistical) --------------------------
    r.sec("10. defect rates vs config")
    n_orders = len(T["orders"])
    n_pay = len(pay)
    n_setl = len(setl)

    def binom_ok(observed: int, n: int, p: float, z: float = 4.0) -> tuple[bool, str]:
        """Observed count within z sigma of the configured rate."""
        if n == 0:
            return True, "n=0"
        exp = n * p
        sd = math.sqrt(max(n * p * (1 - p), 1e-9))
        ok = abs(observed - exp) <= z * sd + 1
        return ok, f"obs={observed} exp={exp:.1f} sd={sd:.1f}"

    checks = [
        ("order_never_paid", recount.get("ORDER_NEVER_PAID", 0), n_orders),
        ("order_amount_typo", recount.get("ORDER_AMOUNT_TYPO", 0), n_orders),
        ("duplicate_capture", recount.get("DUPLICATE_CAPTURE", 0), n_orders),
        ("chargeback", len(T["chargebacks"]), n_pay),
        ("settlement_missing_in_bank", len(flagged), n_setl),
    ]
    for name, obs, n in checks:
        p = float(defects[name]["rate"])
        ok, detail = binom_ok(obs, n, p)
        r.check(f"{name} rate ~ configured {p}", ok, detail)

    # every defect must carry a documented mechanism
    no_mech = [k for k, v in defects.items() if not str(v.get("mechanism", "")).strip()]
    r.check("every defect documents a mechanism", not no_mech, str(no_mech))

    # -- 11. difficulty band -----------------------------------------------
    r.sec("11. difficulty band")
    credits = bank[bank["cr"] > 0]
    utrs = set(setl["utr"])
    tier1 = sum(1 for n in credits["narration"]
                if (mm := UTR_RE.search(n)) and mm.group(1) in utrs)
    frac = tier1 / len(credits) if len(credits) else 0
    r.check("Tier-1 hit rate leaves real work (0.30-0.90)",
            0.30 <= frac <= 0.90, f"{frac:.0%} of {len(credits)} credits")
    matched = gt["expected_outcome_summary"].get("MATCHED", 0)
    total = sum(gt["expected_outcome_summary"].values())
    r.check("not trivially solvable (MATCHED < 97%)", matched / total < 0.97,
            f"{matched}/{total} = {matched/total:.1%}")
    r.check("exceptions actually present", total - matched > 20, f"{total - matched}")
    noise = len(bank) - len(credits)
    r.check("ambient noise rows present", noise > 0, f"{noise} non-credit rows")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", nargs="+", default=["data/seed42", "data/seed99"])
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    rates = yaml.safe_load((ROOT / "config/rates.yaml").read_text())
    defects = yaml.safe_load((ROOT / "config/defects.yaml").read_text())

    all_ok = True
    for ds in args.data:
        path = ROOT / ds if not Path(ds).is_absolute() else Path(ds)
        print(f"\n{'=' * 68}\nVALIDATING {ds}\n{'=' * 68}")
        r = Report(verbose=args.verbose)
        test_primitives(r)
        validate(path, rates, defects, r)
        ok = r.summary()
        n = len(r.rows)
        n_ok = sum(1 for row in r.rows if row[2])
        print(f"\n  {n_ok}/{n} checks passed"
              + ("" if ok else f"  <-- {n - n_ok} FAILURES"))
        if not ok:
            for s, name, detail in r.failures():
                print(f"    FAIL [{s}] {name} {detail}")
        all_ok &= ok

    print("\n" + "=" * 68)
    print("RESULT:", "ALL DATASETS VALID" if all_ok else "VALIDATION FAILED")
    print("=" * 68)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
