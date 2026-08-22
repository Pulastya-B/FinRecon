"""
Money arithmetic in integer paise.

There are no floats in this project's money path, anywhere, deliberately.
0.1 + 0.2 != 0.3 in IEEE-754, and a reconciliation engine that silently
accumulates representation error is producing variances it invented itself.
Every amount is an `int` number of paise; formatting to rupees happens only
at the display boundary.
"""

from __future__ import annotations

PAISE_PER_RUPEE = 100


def rupees_to_paise(rupees: float | int | str) -> int:
    """Convert a rupee amount to integer paise, rounding half-up.

    Accepts str to allow exact decimal input from CSV without a float hop.
    """
    if isinstance(rupees, str):
        rupees = rupees.strip()
        neg = rupees.startswith("-")
        if neg:
            rupees = rupees[1:]
        if "." in rupees:
            whole, frac = rupees.split(".", 1)
            frac = (frac + "00")[:2]
        else:
            whole, frac = rupees, "00"
        value = int(whole or "0") * PAISE_PER_RUPEE + int(frac)
        return -value if neg else value
    return int(round(float(rupees) * PAISE_PER_RUPEE))


def paise_to_rupees_str(paise: int) -> str:
    """Render paise as a plain decimal rupee string, e.g. -1234 -> '-12.34'."""
    sign = "-" if paise < 0 else ""
    p = abs(int(paise))
    return f"{sign}{p // PAISE_PER_RUPEE}.{p % PAISE_PER_RUPEE:02d}"


def format_inr(paise: int) -> str:
    """Human-facing Indian-grouped rupee string, e.g. 18420600 -> '₹1,84,206.00'."""
    sign = "-" if paise < 0 else ""
    p = abs(int(paise))
    whole, frac = divmod(p, PAISE_PER_RUPEE)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return f"{sign}₹{s}.{frac:02d}"


def apply_bps(amount_paise: int, rate_bps: int) -> int:
    """Apply a basis-point rate to an amount, rounding half-up, in integers.

    100 bps == 1.00%. Kept in integer space so the result is reproducible on
    any machine and any Python build -- a float percentage would make the
    generator's output subtly platform-dependent, which would quietly destroy
    the held-out seed guarantee.
    """
    if amount_paise < 0:
        return -apply_bps(-amount_paise, rate_bps)
    return (amount_paise * rate_bps + 5_000) // 10_000


def within_tolerance(a_paise: int, b_paise: int, tolerance_paise: int) -> bool:
    """Absolute-difference comparison with an explicit tolerance band."""
    return abs(a_paise - b_paise) <= tolerance_paise
