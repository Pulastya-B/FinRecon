#!/usr/bin/env python3
"""
Linking facts that more than one tier needs.

This module exists because of a bug that has now happened three times, in
three different shapes:

    shared order-side gates      Tier 2 nearly reimplemented Tier 1's
                                 admission rules and would have admitted a
                                 disputed chain Tier 1 rejects
    contested bank rows          Tier 3 checked candidate uniqueness in one
                                 direction while Tier 2 checked both, so two
                                 batches could each claim the same credit
    UTR pairing                  Tier 3 concluded MISSING_IN_BANK without
                                 consulting a pairing that attribution was
                                 already computing one step later

Every one of them is the same failure: each tier correct on its own, the
pipeline wrong, because a fact lived inside one tier and the tier that needed
it either duplicated it or could not reach it. Importing across tiers does not
fix that -- it creates a cycle (attribution already imports settlement) and it
leaves the ownership question unanswered.

So a fact needed by two tiers lives here, where both reach it and neither owns
it. Nothing in this module decides anything: it answers questions about how
records relate, and the tiers decide what to do with the answers.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from typing import Iterable, Mapping

from .normalize import LedgerEntry, NormalizedLedgers


def index_credits_by_utr(
    ledgers: NormalizedLedgers, claimed_rows: Iterable[str] = ()
) -> dict[str, list[str]]:
    """Unclaimed bank credits, grouped by the UTR Tier 0 recovered.

    Rows whose narration was clipped before the UTR carry None and are absent
    here by construction. They are not lost -- they are simply unreachable by
    reference, which is what Tiers 2 and 3 exist to work around.
    """
    claimed = set(claimed_rows)
    by_utr: dict[str, list[str]] = defaultdict(list)
    for row in ledgers.bank:
        if row.entry_type != "bank_credit" or row.entry_id in claimed:
            continue
        utr = row.references["utr"]
        if utr:
            by_utr[utr].append(row.entry_id)
    return dict(by_utr)


def pair_by_utr(
    ledgers: NormalizedLedgers,
    settlement_ids: Iterable[str],
    claimed_rows: Iterable[str] = (),
) -> dict[str, str]:
    """Pair a batch to a credit on the UTR alone, ignoring the amount.

    Two callers, two different questions, one fact.

    Tier 3 asks it before concluding a payout is MISSING. A batch short by a
    whole refund still ARRIVED -- it arrived light -- and a credit carrying its
    exact UTR is proof of that. Telling an operator to chase a missing payout
    when the money is sitting in the statement is a false exception, and the
    kind that erodes trust in the queue fastest.

    Attribution asks it to get a variance to explain at all, because no tier
    will MATCH a pair whose amounts disagree.

    These pairs are never promoted to matches by either caller. The UTR says
    which payout a credit is; the gap is the thing that still needs an answer,
    and an unexplained gap that became a match is the silent error the whole
    engine is built to avoid.
    """
    by_utr = index_credits_by_utr(ledgers, claimed_rows)
    wanted = set(settlement_ids)

    pairs: dict[str, str] = {}
    for settlement in ledgers.settlements:
        if settlement.entry_id not in wanted:
            continue
        rows = by_utr.get(settlement.references["utr"] or "", [])
        # Exactly one candidate. Two rows carrying one UTR is not a pairing,
        # it is a contradiction, and picking one would be a coin flip.
        if len(rows) == 1:
            pairs[settlement.entry_id] = rows[0]
    return pairs


def zero_value_rows(
    ledgers: NormalizedLedgers, claimed_rows: Iterable[str] = ()
) -> list[LedgerEntry]:
    """Bank rows that moved no money in either direction.

    A cycle whose refunds exceed its gross pays out exactly zero -- the
    shortfall carries into the next cycle rather than the payout going
    negative. The gateway still reports the cycle and the bank still shows the
    line, so the statement carries a row with credit 0 and debit 0.

    Tier 0 types a row as a credit only when credit > 0, so these land as
    debits and never enter any tier's candidate pool. That is what made a
    correct zero payout look like a payout that never arrived.

    Distinguishing them is unambiguous: ambient noise is always a real debit,
    so credit == 0 AND debit == 0 identifies a zero payout and nothing else.
    """
    claimed = set(claimed_rows)
    return [
        row for row in ledgers.bank
        if row.entry_id not in claimed
        and row.amounts["credit_paise"] == 0
        and row.amounts["debit_paise"] == 0
    ]


def pair_zero_payouts(
    ledgers: NormalizedLedgers,
    zero_net_settlement_ids: Iterable[str],
    claimed_rows: Iterable[str] = (),
) -> dict[str, str]:
    """Pair cycles that owed nothing to the zero-value rows that paid nothing.

    Only cycles whose EXPECTED net is zero are eligible, and only rows that
    moved no money are candidates, so neither side can be confused with a real
    payout. Within that, the UTR settles it; where the narration was clipped,
    a single zero row on the payout's own date does.

    Uniqueness is enforced in both directions, like every other pairing here.
    Two cycles owing nothing on the same day, with no UTR to tell their rows
    apart, are genuinely indistinguishable -- and the caller must report that
    as ambiguity, not as money that never came.
    """
    wanted = list(dict.fromkeys(zero_net_settlement_ids))
    if not wanted:
        return {}

    pool = zero_value_rows(ledgers, claimed_rows)
    if not pool:
        return {}

    by_utr: dict[str, list[str]] = defaultdict(list)
    undated: dict[date, list[str]] = defaultdict(list)
    for row in pool:
        utr = row.references["utr"]
        if utr:
            by_utr[utr].append(row.entry_id)
        else:
            # No reference survived the narration clip; the date is all there is.
            undated[row.event_date].append(row.entry_id)

    settlements = {s.entry_id: s for s in ledgers.settlements}
    proposed: dict[str, str] = {}
    for settlement_id in wanted:
        settlement = settlements.get(settlement_id)
        if settlement is None:
            continue
        rows = by_utr.get(settlement.references["utr"] or "", [])
        if len(rows) != 1:
            rows = undated.get(settlement.event_date, [])
        if len(rows) == 1:
            proposed[settlement_id] = rows[0]

    # One row cannot be two payouts, however little either paid.
    claims = Counter(proposed.values())
    return {
        settlement_id: row_id
        for settlement_id, row_id in proposed.items()
        if claims[row_id] == 1
    }


def credit_for_settlement(
    pairs: Mapping[str, str], settlement_id: str
) -> str | None:
    """The credit paired to this batch by reference, if there is one."""
    return pairs.get(settlement_id)
