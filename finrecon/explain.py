#!/usr/bin/env python3
"""
The explanation layer -- prose for exceptions, and nothing else.

THE HARD CONSTRAINT
-------------------
The language model writes PROSE. It never produces, alters, or verifies a
number. Every figure that appears in an explanation -- amounts, dates, ids, the
variance, the arithmetic behind it -- is computed deterministically by the tiers
below and passed IN. The model's only job is turning a structured record into a
sentence a finance person can act on.

This is not a stylistic preference, it is the reason the rest of the engine is
worth anything. 677 matches at 100% precision come from arithmetic that needs
no verification: the settlement equation either reproduces to the paisa or it
does not. Put a language model anywhere in that path and every one of those 677
needs checking again by a human, because a model that is right 99% of the time
has just introduced a 1% error rate into a process whose entire value is being
exactly right. Reconciliation is a determinism problem. The model belongs at
the edges, describing decisions it did not make.

So the failure mode is designed for rather than hoped against:

    model unavailable  -> exceptions ship with template prose
    model hallucinates -> number verification rejects, template prose
    key absent         -> NullProvider, template prose

In all three cases the DECISIONS are byte-identical. Only the wording degrades.

Number verification is mechanical, not aspirational. After generation every
rupee figure and every entity id in the prose is checked against the record it
was given. One token that is not in the record rejects the whole response and
falls back to the template, and the rejection is counted -- that count is a
reported metric, because a model inventing figures is a finding, not noise.

Caching
-------
Every explanation is generated once and stored, keyed by a hash of the input
record. The demo is hosted publicly and must make ZERO API calls in the normal
path: no quota burn, no rate limit part-way through a review, no dependency on
a third party being up while someone is looking at it. The cache is committed.

Run:
    python -m finrecon.explain --data data/seed42
    python -m finrecon.explain --data data/seed42 --offline
    python -m finrecon.explain --data data/seed42 --regenerate-cache
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .money import format_inr, paise_to_rupees_str
from .normalize import NormalizedLedgers, load

ROOT = Path(__file__).resolve().parents[1]
CACHE_ROOT = ROOT / "cache" / "explanations"

DEFAULT_MODEL = "mistral-small-latest"

# Entity ids the model is allowed to mention: every one must be in the record.
ID_RE = re.compile(r"\b(?:ord|pay|rfnd|cb|setl|bank)_[0-9A-Za-z_]+\b")

# Rupee figures, in the shapes prose actually uses. Matched broadly on purpose
# -- a verifier that only catches well-formed amounts is not a verifier.
MONEY_RE = re.compile(
    r"(?:Rs\.?|₹|INR)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
    r"|([0-9][0-9,]*\.[0-9]{2})\b"
)


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------
class Provider(Protocol):
    """One method, so swapping vendors is a config change, not a rewrite."""

    name: str

    def generate(self, prompt: str) -> str:
        ...


class NullProvider:
    """Returns nothing, so the caller uses its template.

    Used when no API key is present and in every test. The suite must never
    make a network call: a test that depends on a third party being reachable
    is not a test, it is a status check on someone else's service.
    """

    name = "null"

    def generate(self, prompt: str) -> str:
        return ""


class MistralProvider:
    """Mistral chat completion. Key from MISTRAL_API_KEY, never from a file."""

    name = "mistral"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        self.model = model
        self.name = f"mistral:{model}"
        from mistralai import Mistral  # imported lazily: optional dependency

        self._client = Mistral(api_key=api_key)

    def generate(self, prompt: str) -> str:
        response = self._client.chat.complete(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            # Low but not zero: the task is paraphrase under tight constraints,
            # and creativity here can only invent figures the verifier will
            # reject anyway.
            temperature=0.2,
            max_tokens=400,
        )
        return (response.choices[0].message.content or "").strip()


def load_dotenv(path: Path | None = None) -> None:
    """Read KEY=VALUE lines from .env into the environment, if it exists.

    Twelve lines rather than a dependency, and it never overwrites a variable
    already set -- an explicitly exported key must win over a stale file.
    The file itself is gitignored: a key in git history stays there after the
    line is deleted and has to be rotated.
    """
    env_path = path or (ROOT / ".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def build_provider(offline: bool = False, model: str = DEFAULT_MODEL) -> Provider:
    """Pick a provider from the environment. Absent key is a normal state."""
    if offline:
        return NullProvider()
    load_dotenv()
    api_key = os.environ.get("MISTRAL_API_KEY", "").strip()
    if not api_key:
        return NullProvider()
    try:
        return MistralProvider(api_key, model)
    except Exception:
        # An unusable provider is the same situation as no provider. It must
        # not be able to stop the pipeline producing exceptions.
        return NullProvider()


# --------------------------------------------------------------------------
# The record -- everything the prose is allowed to say
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ExceptionGroup:
    """One root cause, and every order it affected.

    Grouped by the PAYOUT rather than by the order line, because 229 exception
    rows on seed 42 are 11 actual events. An operator works payouts; handing
    them 229 rows to read is the same information arranged to be unusable.
    """

    group_id: str
    code: str
    settlement_id: str | None = None
    settled_on: str | None = None
    bank_row_id: str | None = None

    expected_net_paise: int | None = None
    actual_credit_paise: int | None = None
    variance_paise: int | None = None

    attribution_level: str | None = None
    attribution_outcome: str | None = None
    item_id: str | None = None
    item_kind: str | None = None
    item_amount_paise: int | None = None
    item_created_at: str | None = None
    item_payment_id: str | None = None
    item_settlement_id: str | None = None
    candidate_count: int = 0
    candidate_items: tuple[str, ...] = ()
    # Value of the orders in this group. The only money an order-side finding
    # has: there is no settlement, so no expected or actual net exists.
    orders_total_paise: int | None = None

    # Evidence, computed by finrecon/evidence.py and passed through untouched.
    # The model never computes these and never decides the band.
    candidates_searched: int | None = None
    declared_link: bool = False
    expected_accidental_fits: float | None = None
    strength: str | None = None

    order_ids: tuple[str, ...] = ()

    @property
    def n_orders(self) -> int:
        return len(self.order_ids)

    @property
    def is_order_side(self) -> bool:
        """No settlement, so no payout, so no shortfall to describe.

        These findings live entirely on the order side -- an order with no
        payment, an order captured twice. Handing them the payout template
        produced 'Payout of unknown date is short .', which is worse than
        saying nothing: it describes a payout that does not exist, with blanks
        where the money should be.
        """
        return self.settlement_id is None

    @property
    def shortfall_paise(self) -> int:
        """Positive when the payout arrived light. The sign convention is
        variance = actual - expected, so a shortfall is the negation."""
        return -(self.variance_paise or 0)

    def money_values(self) -> list[int]:
        return [
            v for v in (
                self.expected_net_paise,
                self.actual_credit_paise,
                self.variance_paise,
                abs(self.variance_paise) if self.variance_paise is not None else None,
                self.shortfall_paise,
                self.item_amount_paise,
                self.orders_total_paise,
            ) if v is not None
        ]

    def entity_ids(self) -> set[str]:
        ids = {
            i for i in (
                self.settlement_id, self.bank_row_id, self.item_id,
                self.item_payment_id, self.item_settlement_id,
            ) if i
        }
        return ids | set(self.order_ids)

    def record_hash(self) -> str:
        """Stable key for the cache.

        Over the FACTS, not the prose: if any figure changes the explanation is
        stale and must be regenerated, and if nothing changes a rerun must cost
        nothing.
        """
        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def facts(self) -> dict[str, str]:
        """Every figure, pre-formatted. The model receives these and no others."""
        out: dict[str, str] = {
            "exception_code": self.code,
            "orders_affected": str(self.n_orders),
        }
        if self.settlement_id:
            out["payout_id"] = self.settlement_id
        if self.settled_on:
            out["payout_date"] = _human_date(self.settled_on)
        if self.bank_row_id:
            out["bank_row_id"] = self.bank_row_id
        if self.orders_total_paise is not None:
            out["orders_total_amount"] = format_inr(self.orders_total_paise)
        if self.expected_net_paise is not None:
            out["expected_amount"] = format_inr(self.expected_net_paise)
        if self.actual_credit_paise is not None:
            out["credited_amount"] = format_inr(self.actual_credit_paise)
        if self.variance_paise is not None:
            out["shortfall_amount"] = format_inr(abs(self.variance_paise))
            out["direction"] = "short" if self.variance_paise < 0 else "over"
        if self.item_id:
            out["cause_item_id"] = self.item_id
            out["cause_item_kind"] = self.item_kind or "item"
        if self.item_amount_paise is not None:
            out["cause_item_amount"] = format_inr(self.item_amount_paise)
        if self.item_created_at:
            out["cause_item_date"] = _human_date(self.item_created_at)
        if self.item_payment_id:
            out["cause_item_payment_id"] = self.item_payment_id
        if self.item_settlement_id:
            out["cause_item_booked_to"] = self.item_settlement_id
        if self.candidate_count > 1:
            out["candidates_that_fit"] = str(self.candidate_count)
        if self.strength:
            out["evidence_strength"] = self.strength
            # candidates_searched is NOT exposed. It is the size of the search
            # space -- 578,350 pairs on one seed-42 group -- and handing it to
            # the model produced "Review the 578350 candidate entries", which
            # is not an action any operator can take. Three entries fit; that
            # is the number they act on. The search size stays in the evidence
            # record, where it is a measure of how much a fit is worth.
            out["link_declared_by_gateway"] = (
                "yes" if self.declared_link else "no"
            )
            out["proof_status"] = (
                "The gateway's own record links these."
                if self.declared_link
                else "Only the amounts match. This is not proof of cause."
            )
        out["mechanism_to_paraphrase"] = self.mechanism()
        return out

    def mechanism(self) -> str:
        """The one explanation this group is allowed to offer.

        Chosen deterministically from the attribution result, so the model
        paraphrases a mechanism the engine established rather than reaching for
        a plausible-sounding one of its own.
        """
        if self.is_order_side and self.code in ORDER_SIDE_MECHANISMS:
            return ORDER_SIDE_MECHANISMS[self.code]
        if self.code == "MISSING_IN_BANK":
            return MECHANISMS["missing"]
        if self.item_kind in MECHANISMS:
            return MECHANISMS[self.item_kind]
        # Several fits and no way to choose is NOT the same as nothing fitting,
        # and telling an operator "no entry explains this" when three do would
        # send them looking for the wrong thing.
        if self.candidate_count > 1:
            return MECHANISMS["ambiguous"]
        return MECHANISMS["unidentified"]


# The domain mechanism for each cause, stated once. This is knowledge the
# ENGINE holds -- it is in config/defects.yaml and in the templates below -- so
# it is supplied to the model as a fact, exactly like an amount.
#
# Measured reason, not a precaution. With the mechanism left to the model, 5 of
# 9 accepted outputs invented a causal story that number verification cannot
# catch -- including "the gateway has already sent the money, so the gap is on
# the bank side" about a payout whose whereabouts are precisely what is
# unknown. Verifying figures protects the arithmetic and does nothing about
# invented reasoning. The fix is to leave nothing to invent.
MECHANISMS: dict[str, str] = {
    "refund": (
        "Refunds are deducted from the settlement cycle that processes them, "
        "not the cycle of the original sale, so a payout can be reduced by a "
        "refund raised against a much earlier purchase."
    ),
    "chargeback": (
        "The gateway debits a disputed amount when it receives the dispute, but "
        "books it against the cycle in which the dispute is later confirmed, so "
        "the debit and the record of it can fall in different payouts."
    ),
    "payment": (
        "The settlement report books this payment to a different cycle from the "
        "payout its money actually left, so the payout is short by that "
        "payment's net contribution."
    ),
    "ambiguous": (
        "Several combinations of ledger entries add up to this difference and "
        "nothing in the records distinguishes them, so no single explanation "
        "can be given. Picking one would be a guess."
    ),
    "unidentified": (
        "Nothing in the gateway ledger accounts for this difference. No refund, "
        "chargeback or payment matches it, so the cause is not yet identified "
        "and must not be guessed at."
    ),
    "missing": (
        "The gateway reported this payout but no matching bank credit has been "
        "found within the expected clearing window. Whether it was released at "
        "all is not established by the available records."
    ),
}


def _human_date(value: str) -> str:
    """ISO date or timestamp -> '20 July'. Display only; never parsed back."""
    text = value.split("T")[0]
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return text
    return f"{parsed.day} {parsed.strftime('%B')}"


# --------------------------------------------------------------------------
# Number verification -- the mechanical part of the constraint
# --------------------------------------------------------------------------
def _canonical_money(text: str) -> str:
    return text.replace(",", "").replace(" ", "").rstrip(".")


def allowed_money_strings(group: ExceptionGroup) -> set[str]:
    """Canonical forms of every amount the record contains."""
    allowed: set[str] = set()
    if group.candidates_searched is not None:
        allowed.add(str(group.candidates_searched))
    for paise in group.money_values():
        plain = paise_to_rupees_str(abs(paise))
        allowed.add(_canonical_money(plain))
        allowed.add(_canonical_money(plain.split(".")[0]))     # whole rupees
        allowed.add(_canonical_money(format_inr(abs(paise)).lstrip("₹")))
    return allowed


@dataclass
class Verification:
    ok: bool
    offending: list[str] = field(default_factory=list)
    reason: str = ""


def verify_numbers(prose: str, group: ExceptionGroup) -> Verification:
    """Every figure and id in the prose must come from the record.

    Rejection is all-or-nothing on purpose. A response with one invented
    amount is not partially usable -- the reader cannot tell which figure was
    the invented one, so the only safe move is to discard the whole thing and
    show the template.
    """
    allowed = allowed_money_strings(group)
    offending: list[str] = []

    for match in MONEY_RE.finditer(prose):
        token = match.group(1) or match.group(2)
        if token and _canonical_money(token) not in allowed:
            offending.append(token)

    known_ids = group.entity_ids()
    for found in ID_RE.findall(prose):
        if found not in known_ids:
            offending.append(found)

    if offending:
        return Verification(
            ok=False,
            offending=offending,
            reason="figures or ids not present in the record",
        )
    return Verification(ok=True)


# --------------------------------------------------------------------------
# Templates -- the fallback, and the shape the model is asked to match
# --------------------------------------------------------------------------
# Causal assertions the prose may never make. Arithmetic shows a number FITS;
# it does not show it CAUSED anything, and prose that says otherwise converts a
# circumstantial match into a finding the reader will act on without checking.
BANNED_CAUSAL = (
    "caused by",
    "the cause is",
    "the cause was",
    "due to the refund",
    "due to the chargeback",
    "due to the payment",
    "because of the refund",
    "because of the chargeback",
    "because of the payment",
    "responsible for",
    "this proves",
    "confirms that",
    "proves that",
)

# At least one of these must appear when the gateway has NOT declared the link.
# Without it the reader has no way to tell an amount coincidence from the
# gateway's own assertion that two records belong together.
CAVEAT_MARKERS = (
    "not proof", "not proven", "does not prove", "only the amounts match",
    "amounts match", "not confirmed", "remains unverified", "consistent with",
    "needs confirming", "must be confirmed", "to be confirmed",
)


# Order-side findings, described as what they ARE.
#
# These have no settlement, so no expected net, no bank credit and no date.
# Handing them the payout template produced "Payout of unknown date is short ."
# -- a sentence about a payout that does not exist, with blanks where the money
# should be. Each code below names its own count and its own money instead.
ORDER_SIDE_TEMPLATES: dict[str, str] = {
    "ORDER_UNPAID": (
        "{n} orders worth {total} have no payment in the gateway or the "
        "bank."
        "\n"
        "The order was written to the merchant's system but checkout was never "
        "completed. Only a three-way reconciliation surfaces these -- comparing "
        "gateway against bank alone cannot see an order that produced no "
        "payment."
    ),
    "DUPLICATE_PAYMENT": (
        "{n} orders were captured twice, worth {total} across both captures."
        "\n"
        "Each order has two payment IDs. Typically the customer's first attempt "
        "appeared to fail at the browser so they retried, and both succeeded. "
        "Finding them is a lookup; deciding which capture is the real sale is "
        "the work."
    ),
    "AMOUNT_VARIANCE_UNEXPLAINED": (
        "{n} orders record a different value from the amount actually "
        "captured, {total} in total."
        "\n"
        "The order ledger and the gateway disagree about the money. Usually "
        "manual keying in the order system rather than a gateway fault, but "
        "which side is wrong has to be established per order."
    ),
    # Not in the original spec. The reproduction turned up a fourth order-side
    # group -- chargebacks whose chains never reached a payout -- and without a
    # template of its own it would be the one group still rendering blanks.
    "CHARGEBACK_UNPOSTED": (
        "{n} orders carry a gateway dispute the merchant's books have not "
        "recorded, worth {total}."
        "\n"
        "The gateway debits a disputed amount when the dispute is raised, often "
        "before the merchant has recorded it anywhere. The money has already "
        "moved; what is missing is the merchant's own record of it."
    ),
}

# The action line, kept out of the paragraphs above so the wording of the
# finding and the wording of the remedy can change independently.
ORDER_SIDE_ACTIONS: dict[str, str] = {
    "ORDER_UNPAID": (
        "Suggested action: check the checkout log for these orders and close "
        "the ones where payment was never attempted."
    ),
    "DUPLICATE_PAYMENT": (
        "Suggested action: decide which capture is the real sale, refund the "
        "other, and confirm the settlement batch still balances afterwards."
    ),
    "AMOUNT_VARIANCE_UNEXPLAINED": (
        "Suggested action: compare each order's recorded value against the "
        "captured amount and establish which side is wrong before correcting."
    ),
    "CHARGEBACK_UNPOSTED": (
        "Suggested action: post these disputes to the books and confirm each "
        "gateway debit against the payout it was taken from."
    ),
}

# What the model is allowed to say about an order-side finding. Without these
# it fell through to MECHANISMS["unidentified"] -- "nothing in the gateway
# ledger accounts for this difference" -- about a finding that has no
# difference and never touched a payout.
ORDER_SIDE_MECHANISMS: dict[str, str] = {
    "ORDER_UNPAID": (
        "The order was written to the merchant's system but checkout was never "
        "completed, so no payment exists in the gateway or the bank. Only a "
        "three-way comparison can see it: gateway against bank alone cannot "
        "detect an order that produced no payment."
    ),
    "DUPLICATE_PAYMENT": (
        "The customer's first attempt appeared to fail at the browser so they "
        "retried, and both attempts succeeded, leaving one order with two "
        "captured payments."
    ),
    "AMOUNT_VARIANCE_UNEXPLAINED": (
        "The order ledger and the gateway disagree about the value of the same "
        "order. This is usually manual keying in the order system rather than a "
        "gateway fault, but which side is wrong has to be established per order."
    ),
    "CHARGEBACK_UNPOSTED": (
        "The gateway debits a disputed amount when the dispute is raised, often "
        "before the merchant has recorded it anywhere, so the money has moved "
        "while the merchant's own record of it is still missing."
    ),
}


class IncompleteExplanation(ValueError):
    """A rendered explanation contained an empty slot.

    Raised, not returned. Blank prose is not degraded output -- it is wrong
    output wearing the shape of correct output, and it shipped to the UI for a
    whole session because nothing checked. Failing loudly is cheaper than a
    reader deciding the engine does not know what it found.
    """


# Signatures of a slot that formatted to nothing. Plain substrings, because an
# escaped pattern silently became a backspace byte once already and a verifier
# that can be defeated by its own escaping is not a verifier.
BLANK_MARKERS = (
    "unknown date",
    "unknown settlement",
    "is short .",
    "an expected .",
    "worth .",
    "totalling .",
    "credited  ",
    " Rs .",
)


def assert_complete(prose: str, group: ExceptionGroup) -> str:
    """Reject an explanation with an empty numeric slot. Never ship a blank."""
    if not prose or not prose.strip():
        raise IncompleteExplanation(f"{group.group_id}: empty explanation")
    lowered = prose.lower()
    for marker in BLANK_MARKERS:
        if marker.lower() in lowered:
            raise IncompleteExplanation(
                f"{group.group_id}: empty slot near {marker!r} in {prose[:120]!r}"
            )
    # A double space is what a missing figure looks like once the sentence
    # around it renders: "The bank credited  against an expected".
    for line in prose.split(chr(10)):
        if "  " in line.strip():
            raise IncompleteExplanation(
                f"{group.group_id}: double space from a blank slot in {line[:120]!r}"
            )
    # A currency symbol with no digit after it is a slot that formatted to
    # nothing -- exactly what "credited Rs against" was.
    for index, char in enumerate(prose):
        if char == "\u20b9" and not prose[index + 1:index + 2].isdigit():
            raise IncompleteExplanation(
                f"{group.group_id}: currency symbol with no amount"
            )
    # A space before punctuation is the tail of a slot that rendered empty:
    # "is short ." was exactly this.
    if " ." in prose or " ," in prose:
        raise IncompleteExplanation(
            f"{group.group_id}: punctuation with nothing before it"
        )
    return prose


def _caveat(group: ExceptionGroup) -> str:
    """The not-proven sentence, when the gateway has not declared the link.

    The amounts agreeing is what was established; that the item CAUSED the gap
    is not. Saying so is the difference between a lead and a finding.
    """
    if not group.strength or group.declared_link:
        return ""
    return (
        f" The amounts match, which is not proof of cause -- {group.item_id} is "
        f"booked to another cycle, so the link is not confirmed."
    )


def verify_wording(prose: str, group: ExceptionGroup) -> Verification:
    """Enforce the two wording rules rather than asking for them.

    Asking is what the prompt does. This is what makes it true -- the same
    distinction that moved causal speculation from 5-of-9 to 0-of-11 when the
    mechanism stopped being the model's to invent.
    """
    lowered = prose.lower()
    # Plain substrings, not regex. These are phrases, and an escaped  in a
    # pattern silently became a backspace byte once already -- a verifier that
    # can be defeated by its own escaping is not a verifier.
    offending = [phrase for phrase in BANNED_CAUSAL if phrase in lowered]
    if offending:
        return Verification(
            ok=False, offending=offending,
            reason="asserts cause where only a numeric fit was established",
        )

    # The caveat is required only where a CAUSE IS NAMED. Prose that says
    # "several combinations fit and none can be singled out" already concedes
    # more than the caveat does; demanding it there would reject the most
    # honest output the engine produces.
    if group.item_id and group.strength and not group.declared_link:
        if not any(marker in lowered for marker in CAVEAT_MARKERS):
            return Verification(
                ok=False, offending=["<missing not-proven caveat>"],
                reason="undeclared link stated without a not-proven caveat",
            )
    return Verification(ok=True)


def template_for(group: ExceptionGroup) -> str:
    """Deterministic prose for every exception code.

    Not a placeholder. This is what ships whenever the model is unavailable or
    its output fails verification, so it has to be genuinely usable on its own
    -- same figures, same shape, plainer wording.
    """
    f = group.facts()
    orders = f"{group.n_orders} order{'s' if group.n_orders != 1 else ''}"

    if group.is_order_side:
        body = ORDER_SIDE_TEMPLATES.get(group.code)
        if body is None:
            # A new order-side code with no template must not silently inherit
            # the payout one. Failing loudly is the whole point of this fix.
            raise IncompleteExplanation(
                f"{group.group_id}: no order-side template for {group.code}"
            )
        if group.orders_total_paise is None:
            raise IncompleteExplanation(
                f"{group.group_id}: order-side group has no orders_total_paise"
            )
        rendered = body.format(
            n=group.n_orders,
            total=format_inr(group.orders_total_paise),
        )
        action = ORDER_SIDE_ACTIONS[group.code]
        return assert_complete(rendered + "\n" + action, group)

    if group.code == "MISSING_IN_BANK":
        return (
            f"Payout {f.get('payout_id', '')} of {f.get('payout_date', 'unknown date')} "
            f"has not arrived.\n"
            f"The gateway reported a payout of {f.get('expected_amount', 'an unknown amount')}, "
            f"and no bank credit matching it has been found within the expected "
            f"clearing window. {orders} are waiting on this payout.\n"
            f"Suggested action: confirm with the gateway whether the payout was "
            f"released, and check the bank statement beyond the expected window."
        )

    head = (
        f"Payout of {f.get('payout_date', 'unknown date')} is "
        f"{f.get('direction', 'short')} {f.get('shortfall_amount', '')}."
    )
    body = (
        f"The bank credited {f.get('credited_amount', '')} against an expected "
        f"{f.get('expected_amount', '')}. {orders} sit in this payout."
    )

    if group.item_id and group.item_kind == "refund":
        body += (
            f" This is consistent with refund {f['cause_item_id']} of "
            f"{f.get('cause_item_amount', '')}"
            + (f", issued {f['cause_item_date']}" if 'cause_item_date' in f else "")
            + (f" against payment {f['cause_item_payment_id']}"
               if 'cause_item_payment_id' in f else "")
            + ". Refunds are deducted from the cycle that processes them, not the "
              "cycle of the original sale, so a payout can be reduced by a refund "
              "against a much earlier purchase."
        )
        action = (
            f"Suggested action: verify {f['cause_item_id']} in the refund report "
            f"and confirm it was deducted from this payout."
            + _caveat(group)
        )
    elif group.item_id and group.item_kind == "chargeback":
        body += (
            f" This is consistent with chargeback {f['cause_item_id']} of "
            f"{f.get('cause_item_amount', '')}"
            + (f", raised {f['cause_item_date']}" if 'cause_item_date' in f else "")
            + ". The gateway debits a disputed amount when it receives the dispute, "
              "but books it against the cycle in which the dispute is later "
              "confirmed, so the two can fall in different payouts."
        )
        action = (
            f"Suggested action: confirm chargeback {f['cause_item_id']} against this "
            f"payout and record the dispute in the books."
            + _caveat(group)
        )
    elif group.item_id and group.item_kind == "payment":
        body += (
            f" This is consistent with payment {f['cause_item_id']} of "
            f"{f.get('cause_item_amount', '')}, which the settlement report books "
            f"to a different cycle from the payout its money left."
        )
        action = (
            f"Suggested action: check whether {f['cause_item_id']} appears in this "
            f"payout's settlement report."
            + _caveat(group)
        )
    elif group.candidate_count > 1:
        # Several fits, none distinguishable. Reporting this as "unexplained"
        # would be true but useless; the count is the actionable part.
        body += (
            f" {group.candidate_count} different combinations of ledger entries "
            f"add up to this difference and nothing in the records tells them "
            f"apart, so no single explanation can be given."
        )
        action = (
            "Suggested action: ask the gateway which entries it deducted from "
            "this payout, since the amounts alone cannot separate them."
        )
    else:
        # L5. Says so, and says what to check. Inventing a cause here would be
        # the single most damaging thing this layer could do.
        body += (
            " No refund, chargeback or payment in the gateway ledger accounts for "
            "the difference, so the cause is not yet identified."
        )
        action = (
            "Suggested action: request the gateway's own breakdown for this payout, "
            "and check for a dispute debited but not yet reported."
        )

    return assert_complete(f"{head}\n{body}\n{action}", group)


# --------------------------------------------------------------------------
# Prompting
# --------------------------------------------------------------------------
PROMPT = """You are writing a short note for a finance operator reconciling a \
payment gateway against a bank statement.

Write exactly three parts, as plain prose, no markdown, no bullet points:
1. One headline sentence naming the money and which payout.
2. Two or three sentences explaining the cause in plain language. Explain the \
MECHANISM, not just the fact -- why this kind of difference arises at all.
3. One sentence beginning "Suggested action:" with something concrete.

ABSOLUTE RULES:
- Use ONLY the figures given below. Do not compute, round, convert, total or \
adjust any number. Copy amounts exactly as written, including the separators.
- Do not mention any amount or identifier that does not appear below.
- The cause is GIVEN to you as `mechanism_to_paraphrase`. Use that explanation \
and no other. Do not add reasons of your own, do not say what a bank or a \
gateway "usually" does, and do not assert anything the facts do not state.
- If the mechanism says the cause is not identified, say exactly that. Do not \
offer a possible cause anyway.
- Never repeat a field name or a code from the facts. No "exception_code", no \
"reconciliation engine", no "tier", no "attribution", no underscored words.
- Call each thing what it is: a refund is a refund, a payment is a payment, a \
payout is a payout. Never call any of them an order.
- Never say something CAUSED the shortfall. The amounts agreeing is what was \
established. Write "is consistent with", never "was caused by", "the cause \
is", "this proves" or "confirms that".
- If `link_declared_by_gateway` is "no", you MUST include the substance of \
`proof_status` -- say in plain words that only the amounts match and this is \
not proof of cause. An explanation without that sentence will be discarded.

FACTS:
{facts}

Write the note now."""


def build_prompt(group: ExceptionGroup) -> str:
    facts = "\n".join(f"- {k}: {v}" for k, v in group.facts().items())
    # The prompt asks for a headline naming which payout. An order-side
    # finding has no payout, and asking for one is how a model invents one.
    if group.is_order_side:
        facts += (
            "\n- NOTE: this finding has no payout, no settlement and no bank "
            "credit. Do not mention a payout, a settlement, a credit or a date. "
            "The headline names the order count and orders_total_amount."
        )
    return PROMPT.format(facts=facts)


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------
def cache_file(seed: str, group_id: str, cache_root: Path = CACHE_ROOT) -> Path:
    return cache_root / seed / f"{group_id}.json"


@dataclass
class Explanation:
    group_id: str
    code: str
    prose: str
    source: str            # llm | template | template_after_rejection
    record_hash: str
    provider: str = "null"
    rejected_tokens: list[str] = field(default_factory=list)
    cache_hit: bool = False


@dataclass
class ExplainStats:
    groups: int = 0
    cache_hits: int = 0
    generated: int = 0
    from_llm: int = 0
    from_template: int = 0
    rejections: int = 0
    api_calls: int = 0
    by_code: dict[str, int] = field(default_factory=dict)


def explain_group(
    group: ExceptionGroup,
    provider: Provider,
    seed: str,
    cache_root: Path = CACHE_ROOT,
    regenerate: bool = False,
    offline: bool = False,
    stats: ExplainStats | None = None,
) -> Explanation:
    """Prose for one group: cache, then model, then template. Never fails."""
    stats = stats if stats is not None else ExplainStats()
    stats.groups += 1
    stats.by_code[group.code] = stats.by_code.get(group.code, 0) + 1

    digest = group.record_hash()
    path = cache_file(seed, group.group_id, cache_root)

    if not regenerate and path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        # The hash is over the facts, so a stale entry means the numbers moved
        # and the prose describing them is now wrong.
        # The cache is why the broken prose survived a whole session: written
        # once, served ever after, nothing looking at it again. An entry that
        # cannot pass the completeness gate is treated as a miss and rebuilt.
        complete = True
        try:
            assert_complete(cached.get("prose", ""), group)
        except IncompleteExplanation:
            complete = False
        if cached.get("record_hash") == digest and complete:
            stats.cache_hits += 1
            return Explanation(
                group_id=group.group_id, code=group.code, prose=cached["prose"],
                source=cached.get("source", "template"), record_hash=digest,
                provider=cached.get("provider", "null"), cache_hit=True,
            )

    if offline and not isinstance(provider, NullProvider):
        raise RuntimeError(
            f"--offline: {group.group_id} is not cached and would require an "
            f"API call. Run with --regenerate-cache to build it."
        )

    fallback = template_for(group)
    prose, source, rejected = fallback, "template", []

    raw = ""
    if not isinstance(provider, NullProvider):
        stats.api_calls += 1
        try:
            raw = provider.generate(build_prompt(group))
        except Exception:
            # A provider error is not an engine error. The exception still
            # ships, with template prose.
            raw = ""

    if raw:
        verdict = verify_numbers(raw, group)
        if verdict.ok:
            verdict = verify_wording(raw, group)
        if verdict.ok:
            # A model handed a record with an empty field writes the blank
            # straight through, and every other check would pass it.
            try:
                assert_complete(raw, group)
            except IncompleteExplanation as exc:
                verdict = Verification(False, [str(exc)])
        if verdict.ok:
            prose, source = raw, "llm"
        else:
            # The whole point of the layer, working. Counted and reported.
            stats.rejections += 1
            rejected = verdict.offending
            source = "template_after_rejection"

    stats.generated += 1
    if source == "llm":
        stats.from_llm += 1
    else:
        stats.from_template += 1

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "group_id": group.group_id,
                "code": group.code,
                "record_hash": digest,
                "prose": prose,
                "source": source,
                "provider": provider.name,
                "rejected_tokens": rejected,
                "generated_at": datetime.now().replace(microsecond=0).isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return Explanation(
        group_id=group.group_id, code=group.code, prose=prose, source=source,
        record_hash=digest, provider=provider.name, rejected_tokens=rejected,
    )


# --------------------------------------------------------------------------
# Building groups from a reconciliation result
# --------------------------------------------------------------------------
def build_groups(
    result: Mapping[str, Any], ledgers: NormalizedLedgers
) -> list[ExceptionGroup]:
    """Collapse exception decisions into one record per root cause."""
    attribution = {a["settlement_id"]: a for a in result.get("attributions", [])}
    settlements = {s.entry_id: s for s in ledgers.settlements}
    bank = {b.entry_id: b for b in ledgers.bank}
    orders_by_id = {o.entry_id: o for o in ledgers.orders}
    refunds = {r.entry_id: r for r in ledgers.refunds}
    chargebacks = {c.entry_id: c for c in ledgers.chargebacks}
    payments = {p.entry_id: p for p in ledgers.payments}

    grouped: dict[tuple[str, str], list[dict]] = {}
    for decision in result.get("decisions", []):
        if decision["outcome"] == "MATCHED":
            continue
        sid = (decision.get("settlement_ids") or [None])[0]
        grouped.setdefault((decision["outcome"], sid or "unknown"), []).append(decision)

    groups: list[ExceptionGroup] = []
    for (code, sid), decisions in sorted(grouped.items()):
        settlement = settlements.get(sid)
        bank_row_id = (decisions[0].get("bank_row_ids") or [None])[0]
        credit = bank.get(bank_row_id) if bank_row_id else None
        attr = attribution.get(sid, {})

        expected = actual = variance = None
        if settlement is not None:
            expected = settlement.amounts["net_paise"]
        if credit is not None:
            actual = credit.amounts["credit_paise"]
        if expected is not None and actual is not None:
            variance = actual - expected

        # An order-side group has no settlement, so no expected net and no
        # bank credit -- the value of its orders is the only money it has, and
        # without it its prose has no figure in it at all. Computed only where
        # it is needed: adding it to payout groups would put a second, unrelated
        # total in front of a reader comparing expected against credited.
        orders_total = None
        if settlement is None:
            orders_total = sum(
                orders_by_id[d["order_id"]].amount_paise
                for d in decisions
                if d["order_id"] in orders_by_id
            )

        item_id = attr.get("item_id")
        item_amount = item_created = item_payment = item_booked = None
        if item_id in refunds:
            entry = refunds[item_id]
            item_amount = entry.amount_paise
            item_created = entry.raw_row.get("created_at")
            item_payment = entry.references["payment_id"]
            item_booked = entry.references["settlement_id"]
        elif item_id in chargebacks:
            entry = chargebacks[item_id]
            item_amount = entry.amount_paise
            item_created = entry.raw_row.get("created_at")
            item_payment = entry.references["payment_id"]
            item_booked = entry.references["settlement_id"]
        elif item_id in payments:
            entry = payments[item_id]
            item_amount = entry.amount_paise
            item_created = entry.raw_row.get("captured_at")
            item_booked = entry.references["settlement_id"]

        groups.append(
            ExceptionGroup(
                group_id=f"{code}__{sid}",
                code=code,
                settlement_id=sid if settlement is not None else None,
                settled_on=settlement.event_date.isoformat() if settlement else None,
                bank_row_id=bank_row_id,
                expected_net_paise=expected,
                actual_credit_paise=actual,
                variance_paise=variance,
                attribution_level=attr.get("level"),
                attribution_outcome=attr.get("outcome"),
                item_id=item_id,
                item_kind=attr.get("item_kind"),
                item_amount_paise=item_amount,
                item_created_at=item_created,
                item_payment_id=item_payment,
                item_settlement_id=item_booked,
                candidates_searched=(attr.get("evidence") or {}).get(
                    "candidates_searched"),
                declared_link=bool(
                    (attr.get("evidence") or {}).get("declared_link")),
                expected_accidental_fits=(attr.get("evidence") or {}).get(
                    "expected_accidental_fits"),
                strength=(attr.get("evidence") or {}).get("strength"),
                candidate_count=attr.get("candidate_count", 0),
                candidate_items=tuple(attr.get("candidate_items") or ()),
                orders_total_paise=orders_total,
                order_ids=tuple(sorted(d["order_id"] for d in decisions)),
            )
        )
    return groups


def explain_all(
    result: Mapping[str, Any],
    ledgers: NormalizedLedgers,
    seed: str,
    provider: Provider | None = None,
    cache_root: Path = CACHE_ROOT,
    regenerate: bool = False,
    offline: bool = False,
) -> tuple[list[Explanation], ExplainStats]:
    provider = provider or build_provider(offline=offline)
    stats = ExplainStats()
    out = [
        explain_group(g, provider, seed, cache_root, regenerate, offline, stats)
        for g in build_groups(result, ledgers)
    ]
    return out, stats


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Explain exceptions in plain language.")
    ap.add_argument("--data", default="data/seed42")
    ap.add_argument("--regenerate-cache", action="store_true")
    ap.add_argument("--offline", action="store_true",
                    help="fail loudly rather than call out on a cache miss")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--show", type=int, default=3, help="explanations to print")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # the rupee sign again

    from .pipeline import reconcile

    seed = Path(args.data).name
    ledgers = load(args.data)
    result = reconcile(args.data)
    provider = build_provider(offline=args.offline, model=args.model)

    explanations, stats = explain_all(
        result, ledgers, seed, provider,
        regenerate=args.regenerate_cache, offline=args.offline,
    )

    print(f"\nexplanations for {args.data}")
    print("-" * 66)
    print(f"  provider                     {provider.name}")
    print(f"  exception groups             {stats.groups}")
    print(f"  cache hits                   {stats.cache_hits}")
    print(f"  generated this run           {stats.generated}")
    print(f"  from the model               {stats.from_llm}")
    print(f"  from templates               {stats.from_template}")
    print(f"  API calls                    {stats.api_calls}")
    print(f"  NUMBER-CHECK REJECTIONS      {stats.rejections}")
    print("  by exception code")
    for code, n in sorted(stats.by_code.items()):
        print(f"    {code:<34} {n:>3}")
    print("-" * 66)

    for explanation in explanations[: args.show]:
        print(f"\n[{explanation.code}] {explanation.group_id}  "
              f"(source: {explanation.source})")
        print(explanation.prose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
