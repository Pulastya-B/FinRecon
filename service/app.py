#!/usr/bin/env python3
"""
The HTTP service: the engine's API, plus the built frontend, in one process.

One process and one artifact, on purpose. A judge opening this on a date
nobody is watching should click a URL and have it work -- not start two
servers, not set an environment variable, not discover that the API host is
hardcoded to localhost:5173. The Vite build lands in web/dist and is mounted
here as static files, so `uvicorn service.app:app` is the whole deployment.

Read-only over the engine. The one thing a browser can write is an entry in
the audit log, and nothing in the engine ever reads that back. A
reconciliation decision cannot be altered from the UI.

The reconciliation path makes no model calls. Explanations are served from the
committed cache, falling back to the deterministic template, so the queue, the
cash position and the Evidence page are correct with MISTRAL_API_KEY unset and
make no outbound request.

ONE endpoint is different, deliberately: POST /api/ask calls a model live. It
is given tools that query the engine rather than data to interpret, and every
figure in its reply is checked against a tool result before the answer leaves
this process. Without a key that endpoint returns no_api_key and everything
else carries on unchanged.

Run:
    python -m uvicorn service.app:app --port 8000
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service import cash, data, engine, qa, trace  # noqa: E402

WEB_DIST = ROOT / "web" / "dist"

app = FastAPI(
    title="finrecon",
    description="Three-way reconciliation: orders, gateway, bank.",
    version="1.0.0",
)


class DecisionBody(BaseModel):
    action: str = Field(description="approve | reject | escalate")
    note: str = ""


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
@app.get("/api/seeds")
def list_seeds():
    """Datasets available to run. Held-out seeds are never listed."""
    return {"seeds": engine.available_seeds()}


@app.post("/api/reconcile/{seed}")
def run_reconcile(seed: str, force: bool = False):
    """Run the pipeline. Cached per seed -- clicking Run twice does not wait twice."""
    try:
        run = engine.get_run(seed, force=force)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "seed": run["seed"],
        "cached": run["cached"],
        "elapsed_seconds": run["elapsed_seconds"],
        "summary": run["summary"],
    }


@app.get("/api/exceptions/{seed}")
def list_exceptions(seed: str):
    """The queue: investigable groups, sorted by rupees descending."""
    try:
        run = engine.get_run(seed)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "seed": run["seed"],
        "summary": run["summary"],
        "groups": [
            {
                "group_id": g["group_id"],
                # settlement | orders. The queue sections on this: a payout
                # shortfall and an order-side pattern are worked by different
                # people in different systems.
                "kind": g["kind"],
                "code": g["code"],
                "settlement_id": g["settlement_id"],
                "headline": g["headline"],
                "rupees": g["rupees"],
                "rupees_paise": g["rupees_paise"],
                "affected_chains": g["affected_chains"],
                "suggested_action": g["suggested_action"],
                "evidence_band": g["evidence_band"],
                "identified_by": g["identified_by"],
                "explanation": g["explanation"],
            }
            for g in run["queue"]
        ],
    }


@app.get("/api/exceptions/{seed}/{group_id}")
def exception_detail(seed: str, group_id: str):
    """Full detail: arithmetic, source records, candidates, evidence."""
    try:
        return engine.group_detail(seed, group_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/exceptions/{seed}/{group_id}/decision")
def post_decision(seed: str, group_id: str, body: DecisionBody):
    """Record an operator action. Appends to the audit log and nothing else."""
    try:
        entry = engine.record_decision(seed, group_id, body.action, body.note)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"recorded": entry.__dict__, "audit_size": len(engine.audit_log())}


@app.get("/api/audit/{seed}")
def get_audit(seed: str):
    try:
        return {"entries": engine.audit_log(seed)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/metrics/{seed}")
def get_metrics(seed: str):
    """Full metric set, for the Evidence page."""
    try:
        return engine.metrics(seed)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --------------------------------------------------------------------------
# Raw data
# --------------------------------------------------------------------------
@app.get("/api/data/{seed}")
def list_tables(seed: str):
    """The six CSVs of a seed, with row counts. ground_truth.json is not one."""
    try:
        return {"seed": seed, "tables": data.table_list(engine._seed_dir(seed))}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/data/{seed}/{table}")
def read_table(
    seed: str,
    table: str,
    offset: int = 0,
    limit: int = 50,
    row: str | None = None,
    settlement: str | None = None,
):
    """One window of one CSV, as strings.

    `row` names a row rather than an offset: a deep link from the queue knows
    an order id, not where that order sits in the file. The server resolves it
    and returns the window containing it.

    `settlement` restricts the table to the rows belonging to one payout, which
    is what "25 orders in this payout" links to. Orders carry no settlement id,
    so the server walks order -> payment -> settlement rather than filtering a
    column that does not exist.
    """
    try:
        return data.page(
            engine._seed_dir(seed), table, offset, limit, row, settlement
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/data/{seed}/{table}/{row_id}/links")
def read_links(seed: str, table: str, row_id: str):
    """The same money in the other ledgers, each hop saying how it was made."""
    try:
        return data.links_for(engine._seed_dir(seed), table, row_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --------------------------------------------------------------------------
# Cash position
# --------------------------------------------------------------------------
@app.get("/api/cash/{seed}")
def get_cash(seed: str):
    """Where the released payouts actually are, as of the last statement line.

    A re-presentation of verdicts the engine already reached -- no window
    arithmetic, no projection, no new rule. See service/cash.py.
    """
    try:
        run = engine.get_run(seed)
        return cash.position(engine._seed_dir(seed), run, engine.group_detail)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --------------------------------------------------------------------------
# Interrogation
# --------------------------------------------------------------------------
@app.get("/api/trace/{seed}/{entity_id}")
def get_trace(seed: str, entity_id: str):
    """Why the engine did not match one entity, tier by tier.

    Reads the decline ledger the tiers have always written and the pipeline
    now returns. No tier is re-run and nothing is recomputed: if this endpoint
    and the engine disagree, the endpoint is wrong.
    """
    try:
        run = engine.get_run(seed)
        return trace.trace(engine._seed_dir(seed), run["result"], entity_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/declined/{seed}")
def get_declined(seed: str):
    """Every entity some tier declined, grouped by kind, so the trace is
    discoverable without already knowing an id to ask about."""
    try:
        run = engine.get_run(seed)
        return trace.declinable_ids(run["result"])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    # The finding the user is looking at, so "why did THIS happen" works
    # without anyone typing setl_20260722_019 by hand.
    subject: str | None = Field(default=None, max_length=120)


@app.post("/api/ask/{seed}")
def ask(seed: str, body: Question):
    """Ask the reconciliation run a question, in words.

    THE ONE LIVE MODEL CALL IN THIS SERVICE. Everything else -- the queue, the
    explanations, the Evidence page -- is offline and cached, and stays that
    way. Here the model is given tools that query the engine rather than data
    to interpret, and every figure in its reply is checked against what the
    tools returned before the answer leaves this process.
    """
    try:
        run = engine.get_run(seed)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    ctx = {
        "seed": seed,
        "seed_dir": engine._seed_dir(seed),
        "result": run["result"],
        "run": run,
        "group_detail": engine.group_detail,
    }
    try:
        return qa.ask(seed, body.question.strip(), ctx, subject=body.subject)
    except Exception as exc:
        # A model or network failure must read as a failed question, not a
        # 500 that looks like the engine broke.
        return JSONResponse({
            "ok": False,
            "error": "provider_failed",
            "message": f"{type(exc).__name__}: {exc}",
        })


@app.get("/api/ask/{seed}/suggested")
def suggested(seed: str):
    # _client() constructs the provider, so a missing or broken SDK raises here
    # -- on the request that renders the Ask page. Degrade to the no-key state,
    # which the UI already reports plainly, rather than 500ing the whole page
    # over a dependency problem the other five pages do not care about.
    try:
        key_present = qa._client() is not None
    except Exception:
        key_present = False
    return {"questions": qa.SUGGESTED,
            "questions_for_subject": qa.SUGGESTED_FOR_SUBJECT,
            "model": qa.MODEL,
            "key_present": key_present}


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------
EVIDENCE_REPORT = ROOT / "cache" / "evidence" / "report.json"


@app.get("/api/evidence")
def get_evidence():
    """The Evidence page's data: thirty seeds, a tolerance curve, two tables.

    Read from disk and returned unchanged. Every figure in it was produced by
    eval/build_evidence.py, which is the only side of the wall that may open
    ground_truth.json -- so precision can be reported here without the service
    ever being one import away from the oracle. Nothing is computed per
    request; the file is 50KB and the route is a file read.
    """
    if not EVIDENCE_REPORT.is_file():
        raise HTTPException(
            status_code=503,
            detail="evidence report not built -- run python eval/build_evidence.py",
        )
    return JSONResponse(json.loads(EVIDENCE_REPORT.read_text(encoding="utf-8")))


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "frontend_built": WEB_DIST.is_dir(),
        "evidence_built": EVIDENCE_REPORT.is_file(),
    }


# --------------------------------------------------------------------------
# Frontend
# --------------------------------------------------------------------------
if WEB_DIST.is_dir():
    app.mount(
        "/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets"
    )

    @app.get("/")
    def index():
        return FileResponse(WEB_DIST / "index.html")

    @app.get("/{path:path}")
    def spa(path: str):
        """Serve the app for any non-API path.

        A single-page app owns its own routes; a refresh on /queue must return
        the app, not a 404. API paths are declared above and match first.

        The resolve-and-contain check is not defensive habit, it is a fix. This
        route joined the request path onto web/dist and served whatever landed,
        so GET /../../data/seed42/ground_truth.json returned the answer key with
        a 200. The ground-truth firewall is the reason precision means anything
        in this project, and it was enforced inside the Python import graph
        while an HTTP route walked straight around it. Nothing outside web/dist
        is servable, whatever the path spells.
        """
        try:
            candidate = (WEB_DIST / path).resolve()
            candidate.relative_to(WEB_DIST.resolve())
        except (ValueError, OSError):
            return FileResponse(WEB_DIST / "index.html")
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(WEB_DIST / "index.html")

else:
    @app.get("/")
    def not_built():
        return JSONResponse(
            status_code=503,
            content={
                "error": "frontend not built",
                "fix": "cd web && npm install && npm run build",
            },
        )
