"""
Record shapes for the three ledgers, plus the ground-truth chain.

Note the asymmetry between the sources -- it is the whole point:

  Order    clean, structured, merchant-authored, one row per sale
  Payment  clean, structured, gateway-authored, explicit foreign keys
  Bank     narration text, a UTR that may be truncated away, NET amounts,
           and no foreign key to anything

The linkage is perfect inside the gateway and dies at the bank boundary.
Everything downstream is a consequence of that one fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

ExceptionCode = Literal[
    "MATCHED",
    "ORDER_UNPAID",
    "DUPLICATE_PAYMENT",
    "MISSING_IN_BANK",
    "MISSING_IN_GATEWAY",
    "AMOUNT_VARIANCE_UNEXPLAINED",
    "CHARGEBACK_UNPOSTED",
    "TIMING_PENDING",
    "AMBIGUOUS_MULTI_CANDIDATE",
    "UNKNOWN",
]

PaymentMethod = Literal["card", "netbanking", "wallet", "upi"]


# --------------------------------------------------------------------------
# Source A -- merchant order ledger
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Order:
    order_id: str
    customer_id: str
    order_date: date
    amount_paise: int
    status: str          # placed | cancelled
    currency: str = "INR"


# --------------------------------------------------------------------------
# Source B -- gateway ledger (payments, refunds, disputes, settlements)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Payment:
    payment_id: str
    order_id: str
    captured_at: datetime
    amount_paise: int
    method: PaymentMethod
    fee_paise: int
    tax_on_fee_paise: int
    status: str          # captured | refunded | disputed
    settlement_id: str | None


@dataclass(frozen=True)
class Refund:
    refund_id: str
    payment_id: str
    amount_paise: int
    created_at: datetime
    # The cycle the deduction lands in -- NOT the cycle the original payment
    # settled in. This is the cross-period attribution problem in one field.
    settlement_id: str | None


@dataclass(frozen=True)
class Chargeback:
    chargeback_id: str
    payment_id: str
    amount_paise: int
    created_at: datetime
    settlement_id: str | None


@dataclass(frozen=True)
class Settlement:
    settlement_id: str
    capture_date: date
    settled_on: date
    utr: str
    gross_paise: int
    fee_paise: int
    tax_paise: int
    refund_paise: int
    chargeback_paise: int
    withholding_paise: int
    carry_forward_in_paise: int
    carry_forward_out_paise: int
    net_paise: int

    def recompute_net(self) -> int:
        """The settlement equation, forward.

        Deliberately simple, because net settlement genuinely is additive.
        The engineering is in running this BACKWARDS: given a bank credit and
        a variance, infer which combination of terms explains it.

        Carry-forward signs, since they are easy to get backwards and were:

          carry_forward_in   a shortfall this cycle INHERITS from the previous
                             one. The merchant owes it, so it is DEDUCTED.
          carry_forward_out  a shortfall this cycle could not cover and pushes
                             to the next. It is ADDED BACK here, because the
                             payout was floored at zero rather than going
                             negative -- the identity net = raw + carry_out is
                             what makes the row balance.

        An earlier version had both signs inverted, which paid the merchant
        their own debt: a cycle inheriting a shortfall settled for MORE than
        its gross. Seeds 42 and 99 never produced a negative-net cycle, so the
        bug was invisible until an unseen seed hit one. See FAILURES.md.
        """
        return (
            self.gross_paise
            - self.fee_paise
            - self.tax_paise
            - self.refund_paise
            - self.chargeback_paise
            - self.withholding_paise
            - self.carry_forward_in_paise
            + self.carry_forward_out_paise
        )


# --------------------------------------------------------------------------
# Source C -- bank statement
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class BankRow:
    bank_row_id: str
    txn_date: date
    narration: str       # free text, possibly clipped mid-UTR
    credit_paise: int
    debit_paise: int
    balance_paise: int


# --------------------------------------------------------------------------
# Ground truth
# --------------------------------------------------------------------------
# Outcome precedence, most severe first. Made explicit rather than left to
# assignment order: a chain can attract several defects, and which one the
# scorer should expect must not depend on the sequence the generator happened
# to run its stages in.
OUTCOME_PRECEDENCE: tuple[ExceptionCode, ...] = (
    "ORDER_UNPAID",
    "DUPLICATE_PAYMENT",
    "MISSING_IN_BANK",
    "CHARGEBACK_UNPOSTED",
    "AMOUNT_VARIANCE_UNEXPLAINED",
    "MATCHED",
)


def worst_outcome(a: ExceptionCode, b: ExceptionCode) -> ExceptionCode:
    """Return whichever outcome ranks more severe under OUTCOME_PRECEDENCE."""
    order = {c: i for i, c in enumerate(OUTCOME_PRECEDENCE)}
    return a if order.get(a, 99) <= order.get(b, 99) else b


@dataclass
class Chain:
    """The true order -> payment -> settlement -> bank linkage.

    Only available because we generated the data. A production reconciler
    cannot compute precision, because it has no oracle; it can only report
    coverage and wait for a human to audit. Synthetic data is not a weakness
    here -- it is the only reason a false-positive rate is measurable at all.

    settlement_ids and bank_row_ids are LISTS, not scalars, and that is not
    defensive over-engineering. A duplicate capture can straddle the capture
    cutoff -- customer retries just after midnight -- putting the two payments
    in different settlement cycles, hence different bank credits. An earlier
    version of this class used scalars; the second write silently clobbered
    the first, and ground truth then disagreed with the CSVs for ~1.5% of
    orders. Any matcher that got those right would have been scored WRONG.
    See FAILURES.md 2026-08-21.
    """
    order_id: str | None
    payment_ids: list[str] = field(default_factory=list)
    refund_ids: list[str] = field(default_factory=list)
    chargeback_ids: list[str] = field(default_factory=list)
    settlement_ids: list[str] = field(default_factory=list)
    bank_row_ids: list[str] = field(default_factory=list)
    defects: list[str] = field(default_factory=list)
    expected_outcome: ExceptionCode = "MATCHED"
