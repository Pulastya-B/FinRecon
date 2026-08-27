#!/usr/bin/env python3
"""
Raw CSV access for the Data page. Read-only, paginated, whitelisted.

WHY A WHITELIST AND NOT A PATH
------------------------------
data/seed42/ also contains ground_truth.json, which the matcher must never open
and which no browser may ever be able to fetch. An endpoint that takes a
filename and joins it to a directory is one traversal away from serving it, and
"the frontend only ever asks for the six CSVs" is not a control. FILES below is
the complete set of things this module can read; anything else is a KeyError
before a path is constructed.

WHY PAGINATION IS NOT OPTIONAL
------------------------------
orders.csv is a thousand rows and bank.csv is larger. Shipping a whole table so
the client can highlight one row of it spends the bandwidth and the DOM on 999
rows nobody asked for. The deep link names a row; the server returns the window
that row falls in and says where in that window it is.

Strings only. Every value is returned exactly as it sits in the file --
unparsed, unformatted, unrounded. The Data page exists so a reader can check the
engine against the source, and a viewer that reformats what it displays cannot
settle an argument about what the source says.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# The complete set of readable files. ground_truth.json is deliberately absent:
# it is answers, not data, and eval/ is the only thing allowed to open it.
FILES: dict[str, dict[str, str]] = {
    "orders":      {"file": "orders.csv",              "id": "order_id",      "label": "Orders"},
    "payments":    {"file": "gateway_payments.csv",    "id": "payment_id",    "label": "Gateway payments"},
    "refunds":     {"file": "gateway_refunds.csv",     "id": "refund_id",     "label": "Refunds"},
    "chargebacks": {"file": "gateway_chargebacks.csv", "id": "chargeback_id", "label": "Chargebacks"},
    "settlements": {"file": "gateway_settlements.csv", "id": "settlement_id", "label": "Settlements"},
    "bank":        {"file": "bank.csv",                "id": "bank_row_id",   "label": "Bank statement"},
}

# Columns holding money, so the client can right-align them. Values are still
# returned as the strings the file holds -- this says how to DRAW a value, not
# what it is.
MONEY_COLUMNS = frozenset({
    "amount", "credit", "debit", "balance", "fee", "tax_on_fee", "tax",
    "gross", "refunds", "chargebacks", "withholding",
    "carry_forward_in", "carry_forward_out", "net",
})

_tables: dict[tuple[str, str], dict[str, Any]] = {}


def _read(seed_dir: Path, key: str) -> dict[str, Any]:
    """Load one CSV as strings and index it by its id column."""
    cache_key = (seed_dir.name, key)
    if cache_key in _tables:
        return _tables[cache_key]

    spec = FILES[key]
    path = seed_dir / spec["file"]
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]

    table = {
        "key": key,
        "label": spec["label"],
        "file": spec["file"],
        "id_column": spec["id"],
        "columns": columns,
        "money_columns": [c for c in columns if c in MONEY_COLUMNS],
        "rows": rows,
        "index": {row[spec["id"]]: i for i, row in enumerate(rows)},
    }
    _tables[cache_key] = table
    return table


def table_list(seed_dir: Path) -> list[dict[str, Any]]:
    """The six files, with row counts, for the file switcher."""
    out = []
    for key in FILES:
        table = _read(seed_dir, key)
        out.append({
            "key": key,
            "label": table["label"],
            "file": table["file"],
            "rows": len(table["rows"]),
        })
    return out


def _rows_in_settlement(seed_dir: Path, key: str, settlement_id: str) -> list[dict]:
    """The rows of one table that belong to one payout.

    Orders do not carry a settlement id -- nothing in orders.csv knows which
    payout its money left in. The link runs order -> payment -> settlement, so
    it has to be walked, which is the same walk the engine does and the reason
    a three-way reconciliation is not a spreadsheet formula.
    """
    payments = _read(seed_dir, "payments")
    in_batch = [r for r in payments["rows"] if r["settlement_id"] == settlement_id]
    if key == "payments":
        return in_batch
    if key == "orders":
        wanted = {r["order_id"] for r in in_batch}
        orders = _read(seed_dir, "orders")
        return [r for r in orders["rows"] if r["order_id"] in wanted]
    if key in ("refunds", "chargebacks"):
        source = _read(seed_dir, key)
        return [r for r in source["rows"] if r["settlement_id"] == settlement_id]
    if key == "settlements":
        table = _read(seed_dir, "settlements")
        idx = table["index"].get(settlement_id)
        return [] if idx is None else [table["rows"][idx]]
    return []


def page(
    seed_dir: Path,
    key: str,
    offset: int = 0,
    limit: int = 50,
    row_id: str | None = None,
    settlement: str | None = None,
) -> dict[str, Any]:
    """One window of a table.

    When row_id is given the window is chosen to CONTAIN that row rather than
    to start at it, so an operator following a deep link sees the rows either
    side of it. Landing on a highlighted row pinned to the top of an otherwise
    empty-looking page hides the context that makes it worth looking at.
    """
    if key not in FILES:
        raise KeyError(f"unknown table {key!r}")
    table = _read(seed_dir, key)
    rows = table["rows"]
    if settlement:
        rows = _rows_in_settlement(seed_dir, key, settlement)
    total = len(rows)
    limit = max(1, min(limit, 200))

    found_index = None
    if row_id:
        found_index = (
            {r[table["id_column"]]: i for i, r in enumerate(rows)}.get(row_id)
            if settlement else table["index"].get(row_id)
        )
        if found_index is not None:
            # A third of the way down the window, not at the top of it.
            offset = max(0, found_index - limit // 3)

    offset = max(0, min(offset, max(0, total - 1)))
    window = rows[offset:offset + limit]

    return {
        "key": key,
        "label": table["label"],
        "file": table["file"],
        "id_column": table["id_column"],
        "columns": table["columns"],
        "money_columns": table["money_columns"],
        "total": total,
        "offset": offset,
        "limit": limit,
        "rows": window,
        "settlement": settlement,
        "row_id": row_id,
        "row_index": found_index,
        "row_found": (found_index is not None) if row_id else None,
    }


def _payout_links(seed_dir: Path, key: str, row_id: str) -> dict[str, Any]:
    """A payout, the credit it arrived as, and what it contains.

    A settlement sits above many order chains rather than on one, so it gets
    the payout-shaped answer: the batch, the bank row its UTR appears in, and a
    count of the payments inside it. Listing seventeen payment ids in a side
    panel would be the same wall of bystanders the detail pane just stopped
    printing.
    """
    settlements = _read(seed_dir, "settlements")
    bank = _read(seed_dir, "bank")
    payments = _read(seed_dir, "payments")

    settlement_row = None
    bank_row = None
    if key == "settlements":
        idx = settlements["index"].get(row_id)
        settlement_row = settlements["rows"][idx] if idx is not None else None
    else:
        idx = bank["index"].get(row_id)
        bank_row = bank["rows"][idx] if idx is not None else None
        narration = (bank_row or {}).get("narration") or ""
        # The bank row carries no settlement id; the only link is a UTR sitting
        # inside free text, which is the boundary the whole engine exists to
        # cross.
        for row in settlements["rows"]:
            utr = (row.get("utr") or "").strip()
            if utr and utr in narration:
                settlement_row = row
                break

    links: list[dict[str, Any]] = []
    if settlement_row is None and bank_row is None:
        return {"root": {"table": key, "id": row_id}, "order_id": None, "links": []}

    if settlement_row is not None:
        sid = settlement_row["settlement_id"]
        links.append({
            "table": "settlements", "id": sid, "row": settlement_row,
            "how": "the payout the gateway reported",
        })
        utr = (settlement_row.get("utr") or "").strip()
        if bank_row is None and utr:
            hits = [b for b in bank["rows"] if utr in (b.get("narration") or "")]
            if len(hits) == 1:
                bank_row = hits[0]
            else:
                links.append({
                    "table": "bank", "id": None, "row": None,
                    "how": (f"{len(hits)} bank rows carry UTR {utr}" if hits
                            else f"no bank row carries UTR {utr} -- this payout "
                                 f"has not been found in the statement"),
                })
        elif bank_row is None:
            links.append({
                "table": "bank", "id": None, "row": None,
                "how": "no UTR on the payout -- nothing to match a bank row on",
            })

        members = [r for r in payments["rows"] if r["settlement_id"] == sid]
        links.append({
            "table": "payments", "id": None, "row": None,
            "count": len(members),
            "settlement": sid,
            "how": f"{len(members)} payments booked to this payout",
        })
        links.append({
            "table": "orders", "id": None, "row": None,
            "count": len({r["order_id"] for r in members}),
            "settlement": sid,
            "how": f"{len({r['order_id'] for r in members})} orders behind those payments",
        })
    elif bank_row is not None:
        links.append({
            "table": "bank", "id": bank_row["bank_row_id"], "row": bank_row,
            "how": "the credit as it appears in the statement",
        })
        links.append({
            "table": "settlements", "id": None, "row": None,
            "how": "no payout UTR appears in this narration",
        })

    if bank_row is not None and not any(l["table"] == "bank" and l["id"] for l in links):
        links.insert(1, {
            "table": "bank", "id": bank_row["bank_row_id"], "row": bank_row,
            "how": "bank narration carries the payout UTR",
        })

    return {"root": {"table": key, "id": row_id}, "order_id": None, "links": links}


def links_for(seed_dir: Path, key: str, row_id: str) -> dict[str, Any]:
    """The same money as it appears in the other ledgers.

    Four lookups across four files done for the reader: an order, the payment
    that captured it, the payout that settled that payment, and the bank credit
    that payout arrived as.

    Every hop states HOW it was made. Order-to-payment and payment-to-settlement
    are declared in the gateway's own columns. Settlement-to-bank is not: it is
    a UTR appearing inside free-text narration, and that is the boundary this
    whole engine exists to cross. Drawing all four hops the same way would hide
    the one difference worth showing.
    """
    if key not in FILES:
        raise KeyError(f"unknown table {key!r}")

    orders = _read(seed_dir, "orders")
    payments = _read(seed_dir, "payments")
    settlements = _read(seed_dir, "settlements")
    bank = _read(seed_dir, "bank")
    refunds = _read(seed_dir, "refunds")
    chargebacks = _read(seed_dir, "chargebacks")

    # A payout or a bank row is not on an order's chain -- it is upstream of
    # many of them -- so walking back to "the" order is the wrong question.
    # These two answer the question that fits their shape: which payout is this
    # credit, and what is inside this payout.
    if key in ("settlements", "bank"):
        return _payout_links(seed_dir, key, row_id)

    # Walk back to an order id whatever kind of row we were handed.
    order_id = None
    if key == "orders":
        order_id = row_id
    elif key == "payments" and row_id in payments["index"]:
        order_id = payments["rows"][payments["index"][row_id]]["order_id"]
    elif key in ("refunds", "chargebacks"):
        source = refunds if key == "refunds" else chargebacks
        if row_id in source["index"]:
            pay_id = source["rows"][source["index"][row_id]]["payment_id"]
            if pay_id in payments["index"]:
                order_id = payments["rows"][payments["index"][pay_id]]["order_id"]

    links: list[dict[str, Any]] = []

    if order_id and order_id in orders["index"]:
        links.append({
            "table": "orders",
            "id": order_id,
            "row": orders["rows"][orders["index"][order_id]],
            "how": "the order as the merchant recorded it",
        })

    pay_rows = [r for r in payments["rows"] if r["order_id"] == order_id] if order_id else []
    settlement_ids: list[str] = []
    for row in pay_rows:
        links.append({
            "table": "payments",
            "id": row["payment_id"],
            "row": row,
            "how": "gateway payment, joined on order_id",
        })
        if row["settlement_id"]:
            settlement_ids.append(row["settlement_id"])

    for pay in pay_rows:
        for source, name in ((refunds, "refunds"), (chargebacks, "chargebacks")):
            for row in source["rows"]:
                if row["payment_id"] == pay["payment_id"]:
                    links.append({
                        "table": name,
                        "id": row[source["id_column"]],
                        "row": row,
                        "how": f"{name[:-1]} raised against this payment",
                    })

    seen: set[str] = set()
    for sid in settlement_ids:
        if sid in seen or sid not in settlements["index"]:
            continue
        seen.add(sid)
        row = settlements["rows"][settlements["index"][sid]]
        links.append({
            "table": "settlements",
            "id": sid,
            "row": row,
            "how": "payout the gateway booked this payment to",
        })
        # The bank hop. Matched only when exactly one row carries the UTR: two
        # rows with one UTR is a contradiction, and picking one is a coin flip.
        utr = (row.get("utr") or "").strip()
        if not utr:
            links.append({
                "table": "bank", "id": None, "row": None,
                "how": "no UTR on the payout -- nothing to match a bank row on",
            })
            continue
        hits = [b for b in bank["rows"] if utr in (b.get("narration") or "")]
        if len(hits) == 1:
            links.append({
                "table": "bank",
                "id": hits[0]["bank_row_id"],
                "row": hits[0],
                "how": f"bank narration carries the payout UTR {utr}",
            })
        else:
            links.append({
                "table": "bank", "id": None, "row": None,
                "how": (f"{len(hits)} bank rows carry UTR {utr}" if hits
                        else f"no bank row carries UTR {utr}"),
            })

    return {"root": {"table": key, "id": row_id}, "order_id": order_id, "links": links}
