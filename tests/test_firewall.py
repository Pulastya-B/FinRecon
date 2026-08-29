#!/usr/bin/env python3
"""
The ground-truth firewall.

CLAUDE.md: "ground_truth.json is readable ONLY by eval/. The matcher must never
open it. If it leaks into the reconciliation path, precision is meaningless."

That is the claim the whole project rests on, and it is the one whose failure
would be silent -- a leak does not crash anything, it just makes 100.00%
precision a lie. So it gets a test, and the test lives in the repo.

This existed once as a scratchpad probe and was lost when the temp directory was
pruned. Restored here because a control that does not survive the session is not
a control.

Two of these checks caught real defects:

  - An earlier version of the import check was a line-oriented string search. It
    reported three violations that were two lines of DOCSTRING and one
    deliberately function-local import inside pipeline.main(). Grep cannot tell
    prose from code. It now asks the interpreter what actually got imported.
  - GET /../../data/seed42/ground_truth.json once returned HTTP 200 with the
    answer key. The containment fix is in service/app.py's spa() handler and the
    traversal cases below are its regression test.

Runs offline. The HTTP section skips if the service is not up, so the core
import checks always run.

Run:
    python tests/test_firewall.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"

fails: list[str] = []


def check(name, ok, detail=""):
    print(f"  [{'pass' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def _imports_eval(module: str) -> str:
    """What eval modules end up in sys.modules after importing `module`?"""
    probe = (
        "import sys; sys.path.insert(0, r'{root}'); import {mod}; "
        "print([m for m in sys.modules if m == 'eval' or m.startswith('eval.')])"
    ).format(root=ROOT, mod=module)
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, cwd=str(ROOT))
    if out.stdout.strip():
        return out.stdout.strip().splitlines()[-1]
    return f"ERROR: {out.stderr.strip()[-200:]}"


print("=== does anything in the serving path import the oracle? ===")
check("importing service.app loads no eval module",
      _imports_eval("service.app") == "[]", _imports_eval("service.app"))
check("importing finrecon.pipeline loads no eval module",
      _imports_eval("finrecon.pipeline") == "[]", _imports_eval("finrecon.pipeline"))
check("importing service.qa loads no eval module",
      _imports_eval("service.qa") == "[]", _imports_eval("service.qa"))

# Positive control. If this comes back empty the detector is blind and every
# result above is meaningless.
control = _imports_eval("eval.score")
check("CONTROL: the detector sees eval when it IS imported",
      "eval.score" in control, control)

print()
print("=== the file the firewall is about ===")
gt = ROOT / "data/seed42/ground_truth.json"
check("CONTROL: ground_truth.json exists to be leaked", gt.is_file())

print()
print("=== does any committed artifact carry ground-truth records? ===")
report = ROOT / "cache/evidence/report.json"
if report.is_file():
    blob = report.read_text(encoding="utf-8")
    leaked_keys = [k for k in ("chains", "expected_outcome", "order_id", "item_ids")
                   if f'"{k}"' in blob]
    check("the served evidence report carries only derived figures",
          not leaked_keys, str(leaked_keys))
else:
    print("  [skip] cache/evidence/report.json not built")

print()
print("=== HTTP: can ground truth be reached over the wire? ===")
try:
    urllib.request.urlopen(f"{BASE}/api/health", timeout=5).read()
    up = True
except Exception:
    up = False

if not up:
    print(f"  [skip] service not running at {BASE}")
    print("         start it with: py -m uvicorn service.app:app --port 8000")
else:
    needle = json.dumps(json.loads(gt.read_text(encoding="utf-8")))[:60]
    traversals = [
        "/../../data/seed42/ground_truth.json",
        "/api/data/seed42/ground_truth",
        "/%2e%2e/%2e%2e/data/seed42/ground_truth.json",
        "/data/seed42/ground_truth.json",
        "/..%2f..%2fdata%2fseed42%2fground_truth.json",
    ]
    leaked = []
    for path in traversals:
        try:
            with urllib.request.urlopen(BASE + path, timeout=15) as r:
                body = r.read().decode("utf-8", "replace")
        except Exception as exc:
            body = str(exc)
        if needle[:40] in body:
            leaked.append(path)
    check("no path serves ground truth", not leaked, str(leaked))

    # The held-out seed must be unreachable by construction, not by filtering.
    for route in ("/api/exceptions/seed99", "/api/cash/seed99",
                  "/api/data/seed99/orders", "/api/trace/seed99/setl_1"):
        try:
            with urllib.request.urlopen(BASE + route, timeout=15) as r:
                code = r.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        except Exception:
            code = -1
        check(f"seed 99 is not served: {route}", code == 404, f"HTTP {code}")

print()
if fails:
    print(f"FIREWALL: BREACHED ({len(fails)}): " + ", ".join(fails))
    sys.exit(1)
print("FIREWALL: INTACT")
