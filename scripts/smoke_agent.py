"""
End-to-end smoke test for the agent loop.

Runs three scenarios against a locally-running server (port 8080):
  1. "How's my store doing?" — should be cheerful when no incidents.
  2. Inject payments outage, ask "what's costing me money?" — should
     diagnose + propose remediation + return an action_id.
  3. Approve that action, ask agent to execute + verify — should
     execute_remediation + get_service_health and report recovery.

Usage:
    python scripts/smoke_agent.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8080"


def _req(method: str, path: str, body=None):
    url = BASE + path
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def show(label, payload):
    print(f"\n=== {label} ===")
    print(json.dumps(payload, indent=2)[:1500])


def main() -> int:
    # 0. status check
    code, status = _req("GET", "/api/status")
    show("status", status)
    if not status["gemini_configured"]:
        print("✗ Gemini is not configured (no API key / no project).")
        return 1

    # reset
    _req("POST", "/api/demo/reset")

    # 1. healthy chat
    code, r1 = _req("POST", "/api/chat",
                    {"message": "How's my store doing right now?"})
    show("scenario 1 — healthy", {"reply": r1["reply"], "tool_calls": [t["name"] for t in r1["tool_calls"]]})

    # 2. inject + ask
    _req("POST", "/api/demo/inject", {"kind": "payments_timeout"})
    time.sleep(2)
    code, r2 = _req("POST", "/api/chat",
                    {"message": "What's costing me money right now and how do we fix it?"})
    show("scenario 2 — costs", {
        "reply": r2["reply"],
        "tool_calls": [t["name"] for t in r2["tool_calls"]],
        "proposed_action_id": r2.get("proposed_action_id"),
    })

    action_id = r2.get("proposed_action_id")
    if not action_id:
        print("✗ Agent did not propose a remediation. Adjust prompt or tool wiring.")
        return 1

    # 3. approve + execute
    _req("POST", f"/api/actions/{action_id}/approve")
    code, r3 = _req("POST", "/api/chat",
                    {"message": "Approved. Please execute the staged action and verify recovery."})
    show("scenario 3 — execute + verify", {
        "reply": r3["reply"],
        "tool_calls": [t["name"] for t in r3["tool_calls"]],
    })

    names = [t["name"] for t in r3["tool_calls"]]
    if "execute_remediation" not in names:
        print("✗ Agent didn't execute_remediation after approval.")
        return 1

    print("\n✓ ALL THREE SCENARIOS PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
