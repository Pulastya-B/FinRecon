#!/usr/bin/env python3
"""
The cash position, as of the last line in the bank statement.

NOT a forecast. The data is historical and static -- the statement ends on a
fixed date -- and calling a re-presentation of past state a forecast invites
backtest error, calibration intervals and a MAPE this project has no harness
for and no business claiming. What this reports is a fact about the pipeline
on a stated date: of the payouts the gateway says it released, which ones are
in the bank, which are late, and which arrived short.

VIEW LAYER ONLY. Every bucket is a re-presentation of a verdict the engine
already reached; nothing here decides anything.

    Confirmed   tied to a bank credit by some tier
    Expected    inside the posting window, not yet arrived   TIMING_PENDING
    At risk     window passed, nothing arrived               MISSING_IN_BANK
    Disputed    arrived, but short of the rebuilt net        AMOUNT_VARIANCE_*
                                                             CHARGEBACK_UNPOSTED

The window arithmetic that separates "late" from "missing" is Tier 3's, done
once, in the engine. If this file ever needs its own rule about when a payout
is overdue, that is the signal it has stopped being a view.
"""

from __future__ import annotations

from typing import Any

from finrecon.money import format_inr
from finrecon.normalize import load

# Queue codes that mean the money ARRIVED but not all of it. Both are payout
# shortfalls; they differ only in whether the engine could name the cause.
SHORT_CODES = frozenset({"AMOUNT_VARIANCE_UNEXPLAINED", "CHARGEBACK_UNPOSTED"})

BUCKETS = (
    ("confirmed", "Confirmed", "credited in full and tied to a bank line"),
    ("expected", "Expected", "inside the posting window, not yet arrived"),
    ("at_risk", "At risk", "the window has passed and nothing arrived"),
    ("disputed", "Disputed", "arrived, but short of what the payout rebuilds to"),
)


def position(seed_dir, run: dict[str, Any], group_detail) -> dict[str, Any]:
    """Assemble the cash position from a completed run."""
    ledgers = load(seed_dir)
    settlements = {s.entry_id: s for s in ledgers.settlements}

    # The statement's own last line. Every "days late" figure on this page is
    # measured against this and nothing else -- using today's date would make
    # the page say something different every morning about a fixed dataset.
    as_of = max((b.timestamp.date() for b in ledgers.bank), default=None)

    # What the engine decided per payout, read off its own queue.
    finding_for: dict[str, dict[str, Any]] = {}
    for group in run["queue"]:
        sid = group.get("settlement_id")
        if sid:
            finding_for[sid] = group

    rows: list[dict[str, Any]] = []
    for sid, entry in settlements.items():
        net = entry.amounts.get("net_paise", entry.amount_paise)
        settled_on = entry.timestamp.date()
        days = (as_of - settled_on).days if as_of else None
        group = finding_for.get(sid)

        if group is None:
            bucket, at_stake, note = "confirmed", 0, "credited in full"
        elif group["code"] == "MISSING_IN_BANK":
            bucket, at_stake = "at_risk", net
            note = (f"settled {settled_on:%d %b}, no credit in "
                    f"{days} days of statement")
        elif group["code"] == "TIMING_PENDING":
            bucket, at_stake, note = "expected", net, "still inside the posting window"
        elif group["code"] in SHORT_CODES:
            bucket, at_stake = "disputed", group["rupees_paise"]
            note = "arrived short by " + group["rupees"]
        else:
            # A payout-side code this file does not know about. Named rather
            # than swept into a bucket it may not belong in.
            bucket, at_stake = "disputed", group["rupees_paise"]
            note = f"payout finding: {group['code']}"

        rows.append({
            "settlement_id": sid,
            "bucket": bucket,
            "settled_on": settled_on.isoformat(),
            "days_since": days,
            "net_paise": net,
            "net": format_inr(net),
            "at_stake_paise": at_stake,
            "at_stake": format_inr(at_stake) if at_stake else None,
            "note": note,
            "group_id": group["group_id"] if group else None,
            "code": group["code"] if group else None,
        })

    summary = []
    for key, label, meaning in BUCKETS:
        members = [r for r in rows if r["bucket"] == key]
        # For confirmed payouts the number that matters is the money that
        # LANDED; for the rest it is the money that did not.
        paise = (sum(r["net_paise"] for r in members) if key == "confirmed"
                 else sum(r["at_stake_paise"] for r in members))
        summary.append({
            "key": key, "label": label, "meaning": meaning,
            "payouts": len(members),
            "paise": paise,
            "amount": format_inr(paise),
        })

    at_risk = next(b for b in summary if b["key"] == "at_risk")
    rows.sort(key=lambda r: (-r["at_stake_paise"], r["settled_on"]))

    return {
        "as_of": as_of.isoformat() if as_of else None,
        "as_of_label": f"as of the last statement line, {as_of:%d %B %Y}" if as_of else "",
        "payouts_total": len(rows),
        "buckets": summary,
        # The sentence a finance person actually acts on. Stated once, in the
        # engine's own figures, rather than left for the reader to assemble.
        "headline": (
            f"{at_risk['amount']} should be in the account and is not."
            if at_risk["payouts"] else
            "Every payout the gateway released is accounted for in the statement."
        ),
        "rows": rows,
        "note": (
            "A position, not a forecast. The statement is historical and ends "
            "on the date above; nothing here projects forward. Each bucket is "
            "a verdict the reconciliation engine already reached."
        ),
    }
