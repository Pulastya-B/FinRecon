"""
Three-way synthetic ledger generator.

Produces orders.csv, gateway_*.csv, bank.csv and ground_truth.json for a
merchant on a payment gateway, with defects injected at documented rates via
documented mechanisms.

Design commitments
------------------
1. Integer paise throughout. No float ever touches a money value.
2. Fully seeded. Same seed + same config == byte-identical output, on any
   machine. This is what makes a held-out seed meaningful: if generation
   drifted, the held-out number would be measuring the generator, not the
   matcher.
3. Every injected defect is recorded in ground truth, WITH its expected
   outcome. Crucially, a defect does not imply an exception -- a truncated
   narration is still a resolvable match, and the engine is expected to
   resolve it. Only genuinely unresolvable items carry an exception code.
   That distinction is what makes exception-accuracy measurable.
4. Ambient noise rows (salary, vendor, tax outflows) are present because a
   real bank statement is not a settlement report. The reconciler must ignore
   them without flagging them, which is its own small test.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from .bizcal import BusinessCalendar
from .money import apply_bps, paise_to_rupees_str
from .schema import (
    BankRow,
    Chain,
    Chargeback,
    Order,
    Payment,
    Refund,
    Settlement,
    worst_outcome,
)

# Narration templates. Real statements vary by bank and by rail; the point is
# that the reference is embedded in free text, not in a typed column.
NARRATION_TEMPLATES = [
    "NEFT-RAZORPAY SOFTWARE PRIVATE LIMITED-{utr}-SETTLEMENT",
    "IMPS/{utr}/RAZORPAY SOFTWARE/PAYOUT",
    "NEFT CR-RATN0000088-RAZORPAY SOFTWARE PVT LTD-{utr}",
    "RTGS-{utr}-RAZORPAYSOFTWARE-MERCHANT SETTLEMENT",
]

NOISE_NARRATIONS = [
    ("SALARY DISBURSAL AUG-{n}", 45_000_00, 320_000_00),
    ("NEFT DR-VENDOR PAYMENT-INV{n}", 8_000_00, 95_000_00),
    ("GST PAYMENT CHALLAN {n}", 20_000_00, 140_000_00),
    ("RENT-PROPERTY MGMT-{n}", 35_000_00, 80_000_00),
    ("UPI/DR/{n}/OFFICE SUPPLIES", 500_00, 12_000_00),
]

NARRATION_CLIP_WIDTH = 35

METHOD_MIX = [
    ("upi", 0.46),
    ("card", 0.31),
    ("netbanking", 0.14),
    ("wallet", 0.09),
]


class LedgerGenerator:
    def __init__(
        self,
        seed: int,
        n_orders: int = 1000,
        start_date: date = date(2026, 7, 1),
        days: int = 45,
        rates_path: str | Path = "config/rates.yaml",
        defects_path: str | Path = "config/defects.yaml",
        defect_overrides: dict[str, float] | None = None,
    ) -> None:
        self.seed = seed
        self.n_orders = n_orders
        self.start_date = start_date
        self.days = days

        self.rates = yaml.safe_load(Path(rates_path).read_text())
        self.defects = yaml.safe_load(Path(defects_path).read_text())

        # Sensitivity sweeps override individual rates without editing the
        # config on disk -- see eval/sensitivity.py.
        if defect_overrides:
            for key, rate in defect_overrides.items():
                if key not in self.defects:
                    raise KeyError(f"unknown defect '{key}'")
                self.defects[key] = {**self.defects[key], "rate": rate}

        holidays = [
            datetime.strptime(h, "%Y-%m-%d").date() if isinstance(h, str) else h
            for h in self.rates.get("holidays_2026", [])
        ]
        self.cal = BusinessCalendar(holidays)
        self.rng = random.Random(seed)

        self.orders: list[Order] = []
        self.payments: list[Payment] = []
        self.refunds: list[Refund] = []
        self.chargebacks: list[Chargeback] = []
        self.settlements: list[Settlement] = []
        self.bank_rows: list[BankRow] = []
        self.chains: list[Chain] = []
        self._orphan_bank: list[dict[str, Any]] = []
        # settlement_id -> the report-vs-bank divergence injected into it.
        #
        # One per cycle, with exactly one exception: compound_shortfall puts
        # TWO on a cycle on purpose. That rule was absolute while L1-L3 were
        # being exercised for the first time, because a gap with two causes
        # would have made a correct engine look wrong. They work on real data
        # now, so the reason to defer is gone and the pair search finally has
        # an input.
        self._batch_shortfalls: dict[str, dict[str, Any]] = {}

    # -- helpers ------------------------------------------------------------

    def _rate(self, name: str) -> float:
        return float(self.defects[name]["rate"])

    def _hit(self, name: str) -> bool:
        """Draw against a defect's rate. Order of calls is fixed, so seeded."""
        return self.rng.random() < self._rate(name)

    def _pick_method(self) -> str:
        r = self.rng.random()
        cum = 0.0
        for method, w in METHOD_MIX:
            cum += w
            if r < cum:
                return method
        return METHOD_MIX[-1][0]

    def _order_amount_paise(self) -> int:
        """Log-ish distribution: many small orders, a thin tail of large ones.

        The tail matters. Exceptions must be ranked by rupee impact, and a
        uniform amount distribution would make that ranking meaningless.
        """
        bucket = self.rng.random()
        if bucket < 0.55:
            rupees = self.rng.randint(199, 2_499)
        elif bucket < 0.88:
            rupees = self.rng.randint(2_500, 14_999)
        elif bucket < 0.98:
            rupees = self.rng.randint(15_000, 79_999)
        else:
            rupees = self.rng.randint(80_000, 450_000)
        return rupees * 100 + self.rng.choice([0, 0, 0, 50, 99])

    def _utr(self) -> str:
        letters = "".join(self.rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(4))
        digits = "".join(str(self.rng.randint(0, 9)) for _ in range(12))
        return f"{letters}{digits}"

    # -- stage 1: orders ----------------------------------------------------

    def _generate_orders(self) -> None:
        for i in range(1, self.n_orders + 1):
            day_offset = self.rng.randint(0, self.days - 1)
            order_date = self.start_date + timedelta(days=day_offset)
            self.orders.append(
                Order(
                    order_id=f"ord_{i:06d}",
                    customer_id=f"cust_{self.rng.randint(1, max(2, self.n_orders // 4)):05d}",
                    order_date=order_date,
                    amount_paise=self._order_amount_paise(),
                    status="placed",
                )
            )
        self.orders.sort(key=lambda o: (o.order_date, o.order_id))

    # -- stage 2: payments --------------------------------------------------

    def _generate_payments(self) -> None:
        pay_seq = 0
        for order in self.orders:
            chain = Chain(order_id=order.order_id)

            if self._hit("order_never_paid"):
                chain.defects.append("ORDER_NEVER_PAID")
                chain.expected_outcome = "ORDER_UNPAID"
                self.chains.append(chain)
                continue

            # Merchant-side keying error: the ORDER row is wrong, the gateway
            # is right. Breaks the assumption that orders are authoritative.
            captured_paise = order.amount_paise
            if self._hit("order_amount_typo"):
                drift = self.rng.choice([-1, 1]) * self.rng.randint(100, 5_000)
                object.__setattr__(order, "amount_paise", order.amount_paise + drift)
                chain.defects.append("ORDER_AMOUNT_TYPO")
                chain.expected_outcome = worst_outcome(
                    chain.expected_outcome, "AMOUNT_VARIANCE_UNEXPLAINED"
                )

            n_captures = 2 if self._hit("duplicate_capture") else 1
            if n_captures == 2:
                chain.defects.append("DUPLICATE_CAPTURE")
                chain.expected_outcome = worst_outcome(
                    chain.expected_outcome, "DUPLICATE_PAYMENT"
                )

            for _ in range(n_captures):
                pay_seq += 1
                lag_hours = self.rng.randint(0, 30)
                captured_at = datetime.combine(order.order_date, time(9, 0)) + timedelta(
                    hours=lag_hours, minutes=self.rng.randint(0, 59)
                )
                method = self._pick_method()
                mdr_bps = int(self.rates["mdr_bps"][method])
                fee = apply_bps(captured_paise, mdr_bps)
                tax = apply_bps(fee, int(self.rates["tax_on_fee_bps"]))

                payment = Payment(
                    payment_id=f"pay_{pay_seq:06d}",
                    order_id=order.order_id,
                    captured_at=captured_at,
                    amount_paise=captured_paise,
                    method=method,
                    fee_paise=fee,
                    tax_on_fee_paise=tax,
                    status="captured",
                    settlement_id=None,
                )
                self.payments.append(payment)
                chain.payment_ids.append(payment.payment_id)

            self.chains.append(chain)

    # -- stage 3: refunds and disputes --------------------------------------

    def _generate_reversals(self) -> None:
        chain_by_order = {c.order_id: c for c in self.chains if c.order_id}
        refund_seq = cb_seq = 0

        for payment in self.payments:
            chain = chain_by_order[payment.order_id]

            is_full = self._hit("full_refund")
            is_partial = (not is_full) and self._hit("partial_refund")

            if is_full or is_partial:
                refund_seq += 1
                amount = (
                    payment.amount_paise
                    if is_full
                    else apply_bps(payment.amount_paise, self.rng.randint(1_000, 7_500))
                )
                # Reversal lands days-to-weeks after capture. It is deducted
                # from whichever cycle processes it -- which is why a batch can
                # be short for reasons living in a different month.
                created_at = payment.captured_at + timedelta(
                    days=self.rng.randint(1, 26), hours=self.rng.randint(0, 20)
                )
                self.refunds.append(
                    Refund(
                        refund_id=f"rfnd_{refund_seq:06d}",
                        payment_id=payment.payment_id,
                        amount_paise=amount,
                        created_at=created_at,
                        settlement_id=None,
                    )
                )
                chain.refund_ids.append(f"rfnd_{refund_seq:06d}")
                chain.defects.append("FULL_REFUND" if is_full else "PARTIAL_REFUND")
                continue

            if self._hit("chargeback"):
                cb_seq += 1
                created_at = payment.captured_at + timedelta(days=self.rng.randint(5, 46))
                self.chargebacks.append(
                    Chargeback(
                        chargeback_id=f"cb_{cb_seq:05d}",
                        payment_id=payment.payment_id,
                        amount_paise=payment.amount_paise,
                        created_at=created_at,
                        settlement_id=None,
                    )
                )
                chain.chargeback_ids.append(f"cb_{cb_seq:05d}")
                chain.defects.append("CHARGEBACK")
                chain.expected_outcome = worst_outcome(
                    chain.expected_outcome, "CHARGEBACK_UNPOSTED"
                )

    # -- stage 4: settlement batching ---------------------------------------

    def _build_settlements(self) -> None:
        lag = int(self.rates["settlement"]["lag_business_days"])
        cutoff_hour = int(self.rates["settlement"]["cutoff_hour"])

        # Capture-date buckets. A payment after the cutoff rolls to the next
        # capture day -- membership is genuinely uncertain near the boundary.
        buckets: dict[date, list[Payment]] = defaultdict(list)
        for p in self.payments:
            cap_date = p.captured_at.date()
            if p.captured_at.hour >= cutoff_hour:
                cap_date = cap_date + timedelta(days=1)
            buckets[cap_date].append(p)

        settle_dates: dict[date, date] = {
            cap: self.cal.add_business_days(cap, lag) for cap in buckets
        }
        ordered_caps = sorted(buckets)
        settlement_of_cap = {
            cap: f"setl_{settle_dates[cap]:%Y%m%d}_{i + 1:03d}"
            for i, cap in enumerate(ordered_caps)
        }

        # Attach each reversal to the first cycle settling on/after its date.
        sorted_by_settle = sorted(ordered_caps, key=lambda c: settle_dates[c])

        def cycle_for(when: date) -> str | None:
            for cap in sorted_by_settle:
                if settle_dates[cap] >= when:
                    return settlement_of_cap[cap]
            return None  # falls outside the window; hits a future cycle

        refunds_by_cycle: dict[str, list[Refund]] = defaultdict(list)
        for i, r in enumerate(self.refunds):
            sid = cycle_for(r.created_at.date())
            self.refunds[i] = Refund(r.refund_id, r.payment_id, r.amount_paise, r.created_at, sid)
            if sid:
                refunds_by_cycle[sid].append(self.refunds[i])

        cb_by_cycle: dict[str, list[Chargeback]] = defaultdict(list)
        for i, c in enumerate(self.chargebacks):
            sid = cycle_for(c.created_at.date())
            self.chargebacks[i] = Chargeback(
                c.chargeback_id, c.payment_id, c.amount_paise, c.created_at, sid
            )
            if sid:
                cb_by_cycle[sid].append(self.chargebacks[i])

        # Stamp settlement_id onto payments.
        pay_index = {p.payment_id: i for i, p in enumerate(self.payments)}
        for cap, plist in buckets.items():
            sid = settlement_of_cap[cap]
            for p in plist:
                i = pay_index[p.payment_id]
                self.payments[i] = Payment(
                    p.payment_id, p.order_id, p.captured_at, p.amount_paise, p.method,
                    p.fee_paise, p.tax_on_fee_paise, p.status, sid,
                )

        wh_cfg = self.rates["withholding"]
        allow_cf = bool(self.rates["settlement"]["allow_carry_forward"])
        carry_in = 0

        for cap in sorted_by_settle:
            sid = settlement_of_cap[cap]
            plist = buckets[cap]
            gross = sum(p.amount_paise for p in plist)
            fee = sum(p.fee_paise for p in plist)
            tax = sum(p.tax_on_fee_paise for p in plist)
            refund_total = sum(r.amount_paise for r in refunds_by_cycle.get(sid, []))
            cb_total = sum(c.amount_paise for c in cb_by_cycle.get(sid, []))
            withholding = (
                apply_bps(gross, int(wh_cfg["rate_bps"])) if wh_cfg["enabled"] else 0
            )

            # carry_in is a debt inherited from the previous cycle: DEDUCT it.
            raw = (
                gross - fee - tax - refund_total - cb_total - withholding - carry_in
            )
            if raw < 0 and allow_cf:
                carry_out = -raw   # shortfall pushed to the next cycle
                net = 0            # payout floored, never negative
            else:
                carry_out = 0
                net = max(raw, 0)

            self.settlements.append(
                Settlement(
                    settlement_id=sid,
                    capture_date=cap,
                    settled_on=settle_dates[cap],
                    utr=self._utr(),
                    gross_paise=gross,
                    fee_paise=fee,
                    tax_paise=tax,
                    refund_paise=refund_total,
                    chargeback_paise=cb_total,
                    withholding_paise=withholding,
                    carry_forward_in_paise=carry_in,
                    carry_forward_out_paise=carry_out,
                    net_paise=net,
                )
            )
            carry_in = carry_out

    # -- stage 4b: report-vs-bank divergence --------------------------------

    def _inject_report_defects(self) -> None:
        """Make the payout disagree with the settlement report that describes it.

        Every gateway CSV stays internally consistent here -- nothing is removed
        from a report and no column is edited. Only the BANK CREDIT changes.

        That is not a shortcut, it is the only shape this defect can take.
        eval/validate.py recomputes gross, fee, tax, refunds, chargebacks and
        withholding for every cycle from the payment/refund/chargeback ledgers,
        so a settlement row that disagreed with those ledgers would be a broken
        dataset rather than an injected defect. The reconciliation problem lives
        exactly where it lives in production: between what the gateway reported
        and what arrived in the bank.

        Each shortfall names an item that still EXISTS in the ledger, attributed
        to a different cycle -- which is what gives attribution something real to
        find. The exception is the silent chargeback, which by definition has no
        counterpart anywhere; that one is unattributable on purpose.
        """
        wh_cfg = self.rates["withholding"]
        wh_bps = int(wh_cfg["rate_bps"]) if wh_cfg["enabled"] else 0

        for s in self.settlements:
            sid = s.settlement_id
            # Nothing to be short of. Skipping keeps a zero-payout cycle from
            # producing a negative credit, which is not a thing a bank does.
            if s.net_paise <= 0:
                continue

            # TWO shortfalls on one cycle, from different ledgers. The only
            # defect that breaks the one-per-batch rule, and deliberately: a
            # gap explained by a refund PLUS a missing payment cannot be
            # matched by any single ledger entry, which is the only way the
            # pair search is ever exercised on real data.
            #
            # Deliberately NOT screened against single-item collisions. Making
            # sure no lone payment happens to equal the sum would be tuning the
            # data so the engine looks right; if a collision occurs, the wrong
            # attribution is real and calibration should report it.
            if self._hit("compound_shortfall"):
                refund_donors = [
                    r for r in self.refunds
                    if r.settlement_id and r.settlement_id != sid
                ]
                payment_donors = [
                    p for p in self.payments if p.settlement_id != sid
                ]
                if refund_donors and payment_donors:
                    refund = self.rng.choice(refund_donors)
                    payment = self.rng.choice(payment_donors)
                    contribution = (
                        payment.amount_paise
                        - payment.fee_paise
                        - payment.tax_on_fee_paise
                        - apply_bps(payment.amount_paise, wh_bps)
                    )
                    total = refund.amount_paise + contribution
                    if 0 < total < s.net_paise:
                        self._batch_shortfalls[sid] = {
                            "cause": "COMPOUND_SHORTFALL",
                            "amount_paise": total,
                            "item_id": None,
                            "item_ids": [refund.refund_id, payment.payment_id],
                            "item_kind": "pair",
                            "expected_outcome": "AMOUNT_VARIANCE_UNEXPLAINED",
                        }
                        continue

            # A refund the report books against another cycle. The row exists,
            # so L2 can find it.
            if self._hit("refund_wrong_cycle"):
                donors = [
                    r for r in self.refunds
                    if r.settlement_id and r.settlement_id != sid
                    and 0 < r.amount_paise < s.net_paise
                ]
                if donors:
                    donor = self.rng.choice(donors)
                    self._batch_shortfalls[sid] = {
                        "cause": "REFUND_WRONG_CYCLE",
                        "amount_paise": donor.amount_paise,
                        "item_id": donor.refund_id,
                        "item_kind": "refund",
                        "expected_outcome": "AMOUNT_VARIANCE_UNEXPLAINED",
                    }
                    continue

            # A dispute booked against the cycle it was CONFIRMED in rather
            # than the one it was debited from. The row exists, so L3 can find
            # it -- the chargeback counterpart of refund_wrong_cycle.
            if self._hit("chargeback_wrong_cycle"):
                donors = [
                    c for c in self.chargebacks
                    if c.settlement_id and c.settlement_id != sid
                    and 0 < c.amount_paise < s.net_paise
                ]
                if donors:
                    donor = self.rng.choice(donors)
                    self._batch_shortfalls[sid] = {
                        "cause": "CHARGEBACK_WRONG_CYCLE",
                        "amount_paise": donor.amount_paise,
                        "item_id": donor.chargeback_id,
                        "item_kind": "chargeback",
                        "expected_outcome": "CHARGEBACK_UNPOSTED",
                    }
                    continue

            # A dispute the merchant's export has never heard of. Deliberately
            # unattributable: there is no row to match, and a shortfall with no
            # ledger counterpart is itself the diagnosis.
            if self._hit("chargeback_silent_deduction"):
                amount = self._order_amount_paise()
                if 0 < amount < s.net_paise:
                    self._batch_shortfalls[sid] = {
                        "cause": "CHARGEBACK_SILENT_DEDUCTION",
                        "amount_paise": amount,
                        "item_id": None,
                        "item_kind": "chargeback",
                        "expected_outcome": "CHARGEBACK_UNPOSTED",
                    }
                    continue

            # A capture the report pages into the wrong cycle. The row exists,
            # so L1 can find it -- matched on its NET contribution, not its
            # gross, because that is what a payout is short by.
            if self._hit("payment_missing_from_report"):
                donors = [
                    p for p in self.payments if p.settlement_id != sid
                ]
                if donors:
                    donor = self.rng.choice(donors)
                    contribution = (
                        donor.amount_paise
                        - donor.fee_paise
                        - donor.tax_on_fee_paise
                        - apply_bps(donor.amount_paise, wh_bps)
                    )
                    if 0 < contribution < s.net_paise:
                        self._batch_shortfalls[sid] = {
                            "cause": "PAYMENT_MISSING_FROM_REPORT",
                            "amount_paise": contribution,
                            "item_id": donor.payment_id,
                            "item_kind": "payment",
                            "expected_outcome": "AMOUNT_VARIANCE_UNEXPLAINED",
                        }

    # -- stage 5: bank statement --------------------------------------------

    def _generate_bank(self) -> None:
        chain_by_order = {c.order_id: c for c in self.chains if c.order_id}

        # Order IDs per settlement, DEDUPED. A chain whose two duplicate
        # captures land in the same cycle must not be visited twice, and a
        # chain whose captures land in DIFFERENT cycles must be visited once
        # per cycle -- which is why the chain accumulates lists.
        settlement_orders: dict[str, list[str]] = defaultdict(list)
        for p in self.payments:
            bucket = settlement_orders[p.settlement_id]
            if p.order_id not in bucket:
                bucket.append(p.order_id)

        def touch(chain: Chain, sid: str, bank_row_id: str | None,
                  defect_list: list[str]) -> None:
            if sid not in chain.settlement_ids:
                chain.settlement_ids.append(sid)
            if bank_row_id and bank_row_id not in chain.bank_row_ids:
                chain.bank_row_ids.append(bank_row_id)
            for dd in defect_list:
                if dd not in chain.defects:
                    chain.defects.append(dd)

        rows: list[tuple[date, BankRow, str | None, list[str]]] = []
        row_seq = 0
        self._settlement_defects: dict[str, list[str]] = defaultdict(list)
        # Authoritative settlement -> bank row mapping. Recorded at settlement
        # level rather than inferred from chains: a chain whose duplicate
        # captures straddle two cycles touches two bank rows, so the chain
        # cannot say which row belongs to which cycle. The scorer needs this
        # link directly.
        self._settlement_bank: dict[str, str | None] = {}

        for s in self.settlements:
            defects: list[str] = []

            if self._hit("settlement_missing_in_bank"):
                defects.append("SETTLEMENT_MISSING_IN_BANK")
                self._settlement_defects[s.settlement_id] = defects
                self._settlement_bank[s.settlement_id] = None
                for oid in settlement_orders.get(s.settlement_id, []):
                    c = chain_by_order[oid]
                    touch(c, s.settlement_id, None, ["SETTLEMENT_MISSING_IN_BANK"])
                    c.expected_outcome = worst_outcome(
                        c.expected_outcome, "MISSING_IN_BANK"
                    )
                continue

            post_date = s.settled_on
            if self._hit("settlement_late_posting"):
                defects.append("LATE_POSTING")
                post_date = self.cal.add_business_days(
                    post_date, self.rng.randint(1, 3)
                )

            # The money that ARRIVED, which is not always the money reported.
            shortfall = self._batch_shortfalls.get(s.settlement_id)
            credit = s.net_paise - (shortfall["amount_paise"] if shortfall else 0)
            if shortfall:
                defects.append(shortfall["cause"])
            if self._hit("rounding_drift"):
                defects.append("ROUNDING_DRIFT")
                credit += self.rng.choice([-2, -1, 1, 2])

            narration = self.rng.choice(NARRATION_TEMPLATES).format(utr=s.utr)
            if self._hit("narration_truncated"):
                defects.append("NARRATION_TRUNCATED")
                narration = narration[:NARRATION_CLIP_WIDTH]

            row_seq += 1
            row = BankRow(
                bank_row_id=f"bank_{row_seq:06d}",
                txn_date=post_date,
                narration=narration,
                credit_paise=max(credit, 0),
                debit_paise=0,
                balance_paise=0,
            )
            rows.append((post_date, row, s.settlement_id, defects))
            self._settlement_defects[s.settlement_id] = defects
            self._settlement_bank[s.settlement_id] = row.bank_row_id

            for oid in settlement_orders.get(s.settlement_id, []):
                touch(chain_by_order[oid], s.settlement_id, row.bank_row_id, defects)
                if shortfall:
                    # Every order in a short batch is affected, because the
                    # shortfall is a property of the payout, not of one order.
                    chain_by_order[oid].expected_outcome = worst_outcome(
                        chain_by_order[oid].expected_outcome,
                        shortfall["expected_outcome"],
                    )

        # Direct NEFT: money with no payment object anywhere. Cannot be solved
        # by tracing an ID, because no ID was ever created.
        n_direct = int(round(len(self.settlements) * self._rate("direct_bank_credit") * 12))
        for _ in range(max(n_direct, 0)):
            row_seq += 1
            d = self.start_date + timedelta(days=self.rng.randint(0, self.days + 4))
            amount = self._order_amount_paise()
            row = BankRow(
                bank_row_id=f"bank_{row_seq:06d}",
                txn_date=d,
                narration=f"NEFT CR-{self._utr()}-CUSTOMER DIRECT TRANSFER",
                credit_paise=amount,
                debit_paise=0,
                balance_paise=0,
            )
            rows.append((d, row, None, ["DIRECT_BANK_CREDIT"]))
            self._orphan_bank.append(
                {
                    "bank_row_id": row.bank_row_id,
                    "defects": ["DIRECT_BANK_CREDIT"],
                    "expected_outcome": "MISSING_IN_GATEWAY",
                }
            )

        # Ambient noise. A bank statement is not a settlement report; the
        # engine must ignore these without flagging them.
        for day_offset in range(0, self.days + 5):
            d = self.start_date + timedelta(days=day_offset)
            if not self.cal.is_business_day(d) or self.rng.random() > 0.55:
                continue
            template, lo, hi = self.rng.choice(NOISE_NARRATIONS)
            row_seq += 1
            row = BankRow(
                bank_row_id=f"bank_{row_seq:06d}",
                txn_date=d,
                narration=template.format(n=self.rng.randint(1000, 9999)),
                credit_paise=0,
                debit_paise=self.rng.randint(lo, hi),
                balance_paise=0,
            )
            rows.append((d, row, None, ["AMBIENT_NOISE"]))

        rows.sort(key=lambda t: (t[0], t[1].bank_row_id))
        balance = 5_000_000_00
        for _, row, _sid, _defects in rows:
            balance += row.credit_paise - row.debit_paise
            self.bank_rows.append(
                BankRow(
                    row.bank_row_id, row.txn_date, row.narration,
                    row.credit_paise, row.debit_paise, balance,
                )
            )

    # -- orchestration ------------------------------------------------------

    def generate(self) -> "LedgerGenerator":
        self._generate_orders()
        self._generate_payments()
        self._generate_reversals()
        self._build_settlements()
        self._inject_report_defects()
        self._generate_bank()
        return self

    # -- output -------------------------------------------------------------

    def write(self, outdir: str | Path) -> Path:
        # Every writer below pins lineterminator="\n". pandas defaults to
        # os.linesep, so the same seed emitted LF on Linux and CRLF on Windows
        # -- which made "byte-identical on any machine" false on half the
        # machines, and quietly so, because the CONTENT was always correct.
        # The determinism test compares bytes, and bytes include the newline.
        out = Path(outdir)
        out.mkdir(parents=True, exist_ok=True)

        pd.DataFrame(
            [
                {
                    "order_id": o.order_id,
                    "customer_id": o.customer_id,
                    "order_date": o.order_date.isoformat(),
                    "amount": paise_to_rupees_str(o.amount_paise),
                    "status": o.status,
                    "currency": o.currency,
                }
                for o in self.orders
            ]
        ).to_csv(out / "orders.csv", index=False, lineterminator="\n")

        pd.DataFrame(
            [
                {
                    "payment_id": p.payment_id,
                    "order_id": p.order_id,
                    "captured_at": p.captured_at.isoformat(),
                    "amount": paise_to_rupees_str(p.amount_paise),
                    "method": p.method,
                    "fee": paise_to_rupees_str(p.fee_paise),
                    "tax_on_fee": paise_to_rupees_str(p.tax_on_fee_paise),
                    "status": p.status,
                    "settlement_id": p.settlement_id or "",
                }
                for p in self.payments
            ]
        ).to_csv(out / "gateway_payments.csv", index=False, lineterminator="\n")

        pd.DataFrame(
            [
                {
                    "refund_id": r.refund_id,
                    "payment_id": r.payment_id,
                    "amount": paise_to_rupees_str(r.amount_paise),
                    "created_at": r.created_at.isoformat(),
                    "settlement_id": r.settlement_id or "",
                }
                for r in self.refunds
            ]
        ).to_csv(out / "gateway_refunds.csv", index=False, lineterminator="\n")

        pd.DataFrame(
            [
                {
                    "chargeback_id": c.chargeback_id,
                    "payment_id": c.payment_id,
                    "amount": paise_to_rupees_str(c.amount_paise),
                    "created_at": c.created_at.isoformat(),
                    "settlement_id": c.settlement_id or "",
                }
                for c in self.chargebacks
            ]
        ).to_csv(out / "gateway_chargebacks.csv", index=False, lineterminator="\n")

        # The settlement report is the bridge across the bank boundary. It is
        # emitted in full here; the interesting production case is when it is
        # incomplete, which sensitivity runs can simulate by dropping rows.
        pd.DataFrame(
            [
                {
                    "settlement_id": s.settlement_id,
                    "capture_date": s.capture_date.isoformat(),
                    "settled_on": s.settled_on.isoformat(),
                    "utr": s.utr,
                    "gross": paise_to_rupees_str(s.gross_paise),
                    "fee": paise_to_rupees_str(s.fee_paise),
                    "tax": paise_to_rupees_str(s.tax_paise),
                    "refunds": paise_to_rupees_str(s.refund_paise),
                    "chargebacks": paise_to_rupees_str(s.chargeback_paise),
                    "withholding": paise_to_rupees_str(s.withholding_paise),
                    "carry_forward_in": paise_to_rupees_str(s.carry_forward_in_paise),
                    "carry_forward_out": paise_to_rupees_str(s.carry_forward_out_paise),
                    "net": paise_to_rupees_str(s.net_paise),
                }
                for s in self.settlements
            ]
        ).to_csv(out / "gateway_settlements.csv", index=False, lineterminator="\n")

        pd.DataFrame(
            [
                {
                    "bank_row_id": b.bank_row_id,
                    "txn_date": b.txn_date.isoformat(),
                    "narration": b.narration,
                    "credit": paise_to_rupees_str(b.credit_paise),
                    "debit": paise_to_rupees_str(b.debit_paise),
                    "balance": paise_to_rupees_str(b.balance_paise),
                }
                for b in self.bank_rows
            ]
        ).to_csv(out / "bank.csv", index=False, lineterminator="\n")

        # newline="\n" and an explicit encoding for the same reason as the CSVs
        # above: write_text() defaults to os.linesep and the locale codec, which
        # made the reproducibility guarantee depend on the host OS.
        (out / "ground_truth.json").write_text(
            json.dumps(self._ground_truth(), indent=2),
            encoding="utf-8",
            newline="\n",
        )
        return out

    def _ground_truth(self) -> dict[str, Any]:
        # NOTE: defect_summary counts CHAINS AFFECTED, not defect events.
        # A chain spanning two truncated settlements counts once, because the
        # scoring question is "is this order affected", not "how many times".
        defect_counts: dict[str, int] = defaultdict(int)
        outcome_counts: dict[str, int] = defaultdict(int)
        for c in self.chains:
            for d in c.defects:
                defect_counts[d] += 1
            outcome_counts[c.expected_outcome] += 1
        for o in self._orphan_bank:
            outcome_counts[o["expected_outcome"]] += 1
            defect_counts["DIRECT_BANK_CREDIT"] += 1

        return {
            "meta": {
                "seed": self.seed,
                "n_orders": self.n_orders,
                "start_date": self.start_date.isoformat(),
                "days": self.days,
                "n_payments": len(self.payments),
                "n_refunds": len(self.refunds),
                "n_chargebacks": len(self.chargebacks),
                "n_settlements": len(self.settlements),
                "n_bank_rows": len(self.bank_rows),
                "gross_paise": sum(p.amount_paise for p in self.payments),
                "configured_defect_rates": {
                    k: v["rate"] for k, v in self.defects.items()
                },
            },
            "chains": [
                {
                    "order_id": c.order_id,
                    "payment_ids": c.payment_ids,
                    "refund_ids": c.refund_ids,
                    "chargeback_ids": c.chargeback_ids,
                    "settlement_ids": c.settlement_ids,
                    "bank_row_ids": c.bank_row_ids,
                    "defects": c.defects,
                    "expected_outcome": c.expected_outcome,
                }
                for c in self.chains
            ],
            "settlements": [
                {
                    "settlement_id": s.settlement_id,
                    "sequence": i + 1,
                    "settled_on": s.settled_on.isoformat(),
                    "bank_row_id": self._settlement_bank.get(s.settlement_id),
                    "expected_net_paise": s.net_paise,
                    "payment_ids": [
                        p.payment_id for p in self.payments
                        if p.settlement_id == s.settlement_id
                    ],
                    "refund_ids": [
                        r.refund_id for r in self.refunds
                        if r.settlement_id == s.settlement_id
                    ],
                    "chargeback_ids": [
                        c.chargeback_id for c in self.chargebacks
                        if c.settlement_id == s.settlement_id
                    ],
                    "defects": self._settlement_defects.get(s.settlement_id, []),
                }
                for i, s in enumerate(self.settlements)
            ],
            "orphan_bank_rows": self._orphan_bank,
            # The oracle for attribution. Records not merely THAT a batch is
            # short but WHICH item caused it, so a scorer can ask whether the
            # engine named the right cause rather than merely named one --
            # the difference between attribution and a plausible-sounding guess.
            "settlement_shortfalls": [
                {
                    "settlement_id": sid,
                    "cause": info["cause"],
                    "amount_paise": info["amount_paise"],
                    "item_id": info["item_id"],
                    # EVERY causing item, so a scorer can ask whether the
                    # engine named the right PAIR rather than merely a pair.
                    # Single-cause shortfalls carry a one-element list; the
                    # reader then has one field to consult, not two shapes.
                    "item_ids": info.get(
                        "item_ids",
                        [info["item_id"]] if info["item_id"] else [],
                    ),
                    "item_kind": info["item_kind"],
                    "expected_outcome": info["expected_outcome"],
                    "attributable": bool(
                        info.get("item_ids") or info["item_id"]
                    ),
                }
                for sid, info in sorted(self._batch_shortfalls.items())
            ],
            "defect_summary": dict(sorted(defect_counts.items())),
            "expected_outcome_summary": dict(sorted(outcome_counts.items())),
        }
