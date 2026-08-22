"""Generator invariants. These must hold for ANY seed.

A generator that quietly violates its own settlement equation would make every
downstream accuracy number meaningless, so these run before anything else.
"""
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from finrecon.money import rupees_to_paise  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


# Money columns are read as STRINGS, deliberately. Letting pandas infer them
# as float64 is precisely the bug this project refuses to ship: "1234.00"
# becomes 1234.0, and any later comparison inherits IEEE-754 error. Read as
# text, convert to integer paise, never touch a float.
MONEY_COLS = {
    "amount", "fee", "tax_on_fee", "credit", "debit", "balance", "gross",
    "tax", "refunds", "chargebacks", "withholding", "carry_forward_in",
    "carry_forward_out", "net",
}


def _read(path):
    head = pd.read_csv(path, nrows=0)
    dtypes = {c: str for c in head.columns if c in MONEY_COLS}
    return pd.read_csv(path, dtype=dtypes)


def load(d):
    p = ROOT / d
    return {
        "orders": _read(p / "orders.csv"),
        "payments": _read(p / "gateway_payments.csv"),
        "refunds": _read(p / "gateway_refunds.csv"),
        "chargebacks": _read(p / "gateway_chargebacks.csv"),
        "settlements": _read(p / "gateway_settlements.csv"),
        "bank": _read(p / "bank.csv"),
        "gt": json.loads((p / "ground_truth.json").read_text()),
    }


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")
    return cond


def main():
    d = load("data/seed42")
    ok = True
    P = rupees_to_paise

    # 1. Settlement equation balances for every cycle.
    bad = []
    for _, s in d["settlements"].iterrows():
        net = (P(s["gross"]) - P(s["fee"]) - P(s["tax"]) - P(s["refunds"])
               - P(s["chargebacks"]) - P(s["withholding"])
               + P(s["carry_forward_in"]) - P(s["carry_forward_out"]))
        if net != P(s["net"]):
            bad.append((s["settlement_id"], net, P(s["net"])))
    ok &= check("settlement equation balances", not bad, f"({len(bad)} broken)")

    # 2. Every payment belongs to exactly one settlement.
    ok &= check("all payments batched",
                d["payments"]["settlement_id"].notna().all()
                and (d["payments"]["settlement_id"] != "").all())

    # 3. Settlement gross equals sum of its member payments.
    pay = d["payments"].copy()
    pay["p"] = pay["amount"].map(P)
    agg = pay.groupby("settlement_id")["p"].sum()
    mismatch = [
        sid for sid, g in agg.items()
        if P(d["settlements"].set_index("settlement_id").loc[sid, "gross"]) != g
    ]
    ok &= check("gross == sum(member payments)", not mismatch, f"({len(mismatch)} off)")

    # 4. Money is integral: no fractional paise anywhere.
    frac = [c for c in ["amount", "fee", "tax_on_fee"]
            if not pay[c].astype(str).str.match(r"^-?\d+\.\d{2}$").all()]
    ok &= check("all amounts 2dp exact", not frac, str(frac))

    # 5. Bank running balance is internally consistent.
    b = d["bank"].copy()
    bal = 5_000_000_00
    drift = 0
    for _, r in b.iterrows():
        bal += P(r["credit"]) - P(r["debit"])
        if bal != P(r["balance"]):
            drift += 1
    ok &= check("bank balance ties", drift == 0, f"({drift} rows drift)")

    # 6. Every refund references a real payment.
    pids = set(d["payments"]["payment_id"])
    ok &= check("refunds reference real payments",
                set(d["refunds"]["payment_id"]).issubset(pids))

    # 7. Ground truth covers every order.
    ok &= check("ground truth covers all orders",
                len(d["gt"]["chains"]) == len(d["orders"]))

    # 8. Determinism: regenerating seed 42 gives identical bytes.
    subprocess.run([sys.executable, "scripts/generate.py", "--seed", "42",
                    "--orders", "1000", "--out", "data/_determinism_check"],
                   cwd=ROOT, capture_output=True, check=True)
    same = True
    for f in ["orders.csv", "gateway_payments.csv", "bank.csv", "ground_truth.json"]:
        a = (ROOT / "data/seed42" / f).read_bytes()
        c = (ROOT / "data/_determinism_check" / f).read_bytes()
        same &= a == c
    ok &= check("seed 42 reproduces byte-identically", same)

    print(f"\n{'ALL INVARIANTS HOLD' if ok else 'INVARIANT VIOLATIONS PRESENT'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
