"""
In-memory mock of a small brick-and-mortar retailer's online storefront.

Simulates 4 services (web, cart, payments, search) producing latency,
error rate, session counts, and revenue. Lets us inject realistic
incidents on demand so the agent has something concrete to investigate
during the demo.

This module is the source of truth that BOTH the UI dashboard and our
synthetic Dynatrace adapter read from. When a real Dynatrace tenant is
configured, the agent queries Dynatrace; otherwise it queries this.
"""

from __future__ import annotations

import math
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Dict, List, Optional

SERVICES = ["web-frontend", "cart-service", "payments-service", "search-service"]

# Baseline behaviour (good day at the store)
BASELINE = {
    "web-frontend":     {"latency_ms": 180, "error_rate": 0.004},
    "cart-service":     {"latency_ms": 240, "error_rate": 0.006},
    "payments-service": {"latency_ms": 320, "error_rate": 0.008},
    "search-service":   {"latency_ms": 90,  "error_rate": 0.002},
}

# Average order value for revenue-impact math
AVG_ORDER_VALUE_USD = 84.50
BASELINE_CHECKOUTS_PER_MIN = 7.4
BASELINE_CONVERSION_RATE = 0.034


@dataclass
class Incident:
    incident_id: str
    title: str
    service: str
    detected_at: datetime
    severity: str           # "critical" | "warning" | "info"
    symptoms: Dict[str, float]
    root_cause: str
    suggested_fix: Dict[str, str]   # {action, target, params}
    resolved_at: Optional[datetime] = None

    def to_summary(self) -> dict:
        return {
            "incident_id": self.incident_id,
            "title": self.title,
            "service": self.service,
            "detected_at": self.detected_at.isoformat(),
            "severity": self.severity,
            "status": "resolved" if self.resolved_at else "open",
            "symptoms": self.symptoms,
            "root_cause": self.root_cause,
        }


class StoreSimulator:
    """Lives in memory. One per process. Thread-safe enough for a demo."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._start = datetime.now(timezone.utc)
        self._incidents: Dict[str, Incident] = {}
        self._action_log: List[dict] = []
        self._approved_actions: Dict[str, dict] = {}

    # ---------- metrics ----------

    def current_metrics(self) -> dict:
        """Snapshot of live storefront metrics, optionally degraded by open incidents."""
        with self._lock:
            services: Dict[str, dict] = {}
            for svc in SERVICES:
                base = BASELINE[svc]
                latency = base["latency_ms"]
                errors = base["error_rate"]
                # apply incident deltas
                for inc in self._incidents.values():
                    if inc.resolved_at or inc.service != svc:
                        continue
                    latency += inc.symptoms.get("latency_delta_ms", 0)
                    errors += inc.symptoms.get("error_rate_delta", 0)
                # gentle jitter
                latency *= 1 + (random.random() - 0.5) * 0.05
                errors = max(0.0, errors * (1 + (random.random() - 0.5) * 0.1))
                services[svc] = {
                    "latency_ms_p95": round(latency, 1),
                    "error_rate":     round(errors, 4),
                    "requests_per_min": int(140 + random.random() * 60),
                }

            # checkout funnel
            open_payments_incident = any(
                i.service == "payments-service" and not i.resolved_at
                for i in self._incidents.values()
            )
            open_cart_incident = any(
                i.service == "cart-service" and not i.resolved_at
                for i in self._incidents.values()
            )

            conv_rate = BASELINE_CONVERSION_RATE
            checkouts_per_min = BASELINE_CHECKOUTS_PER_MIN
            if open_payments_incident:
                conv_rate *= 0.32
                checkouts_per_min *= 0.30
            elif open_cart_incident:
                conv_rate *= 0.65
                checkouts_per_min *= 0.62

            active_sessions = int(820 + 80 * math.sin(time.time() / 120))

            return {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "active_sessions": active_sessions,
                "checkouts_per_min": round(checkouts_per_min, 2),
                "conversion_rate":   round(conv_rate, 4),
                "baseline_conversion_rate": BASELINE_CONVERSION_RATE,
                "baseline_checkouts_per_min": BASELINE_CHECKOUTS_PER_MIN,
                "avg_order_value_usd": AVG_ORDER_VALUE_USD,
                "services": services,
            }

    def revenue_loss_estimate(self, minutes: int) -> dict:
        """Estimate revenue lost over the last `minutes` of degraded checkout.

        The effective degraded window is the *smaller* of the requested
        `minutes` and the time elapsed since the oldest open incident, so
        a freshly-injected incident shows a small (and honest) loss that
        grows as the incident persists.
        """
        m = self.current_metrics()
        open_incs = self.open_incidents()
        if open_incs:
            now = datetime.now(timezone.utc)
            oldest = min(i.detected_at for i in open_incs)
            elapsed = (now - oldest).total_seconds() / 60.0
            effective = max(0.1, min(float(minutes), elapsed))
        else:
            effective = 0.0
        normal = m["baseline_checkouts_per_min"] * AVG_ORDER_VALUE_USD * effective
        actual = m["checkouts_per_min"] * AVG_ORDER_VALUE_USD * effective
        loss = max(0.0, normal - actual)
        return {
            "window_minutes": minutes,
            "effective_degraded_minutes": round(effective, 1),
            "expected_revenue_usd": round(normal, 2),
            "actual_revenue_usd": round(actual, 2),
            "estimated_loss_usd": round(loss, 2),
            "checkouts_lost": max(
                0,
                int((m["baseline_checkouts_per_min"] - m["checkouts_per_min"]) * effective),
            ),
        }

    # ---------- incidents ----------

    def open_incidents(self) -> List[Incident]:
        with self._lock:
            return [i for i in self._incidents.values() if not i.resolved_at]

    def all_incidents(self) -> List[Incident]:
        with self._lock:
            return list(self._incidents.values())

    def get_incident(self, incident_id: str) -> Optional[Incident]:
        with self._lock:
            return self._incidents.get(incident_id)

    def inject_incident(self, kind: str) -> Incident:
        """Create a predefined incident scenario."""
        recipes = {
            "payments_timeout": Incident(
                incident_id=f"INC-{uuid.uuid4().hex[:6].upper()}",
                title="Checkout failing: payment gateway timeouts",
                service="payments-service",
                detected_at=datetime.now(timezone.utc),
                severity="critical",
                symptoms={
                    "latency_delta_ms": 2400,
                    "error_rate_delta": 0.27,
                    "p95_latency_ms": 2720,
                },
                root_cause=(
                    "Primary payment gateway (PayGate-US-East) is returning HTTP 504 "
                    "for ~31% of authorize requests. Average response time has climbed "
                    "from 320 ms to 2.7 s. No code change in last 24h; outage appears "
                    "to be upstream at the gateway provider."
                ),
                suggested_fix={
                    "action": "failover_payment_processor",
                    "target": "payments-service",
                    "params": "Switch primary processor PayGate-US-East -> Stripe-Fallback. "
                              "Estimated time to switch: 45 seconds. No customer impact "
                              "on in-flight carts; new checkouts route to Stripe.",
                },
            ),
            "cart_slow": Incident(
                incident_id=f"INC-{uuid.uuid4().hex[:6].upper()}",
                title="Add-to-cart 4x slower than normal",
                service="cart-service",
                detected_at=datetime.now(timezone.utc),
                severity="warning",
                symptoms={
                    "latency_delta_ms": 1100,
                    "error_rate_delta": 0.04,
                    "p95_latency_ms": 1340,
                },
                root_cause=(
                    "cart-service is hitting a connection-pool ceiling on the inventory "
                    "database. Pool size is configured at 10; saturation is 100% during "
                    "peak hour. Queries themselves are fast (<20 ms); waiting on a free "
                    "connection is the dominant cost."
                ),
                suggested_fix={
                    "action": "scale_connection_pool",
                    "target": "cart-service",
                    "params": "Increase Postgres connection pool from 10 -> 50. "
                              "Rolling restart of cart-service (3 pods). Zero downtime.",
                },
            ),
            "search_outage": Incident(
                incident_id=f"INC-{uuid.uuid4().hex[:6].upper()}",
                title="Product search returning empty results",
                service="search-service",
                detected_at=datetime.now(timezone.utc),
                severity="critical",
                symptoms={
                    "latency_delta_ms": 0,
                    "error_rate_delta": 0.85,
                    "p95_latency_ms": 90,
                },
                root_cause=(
                    "Search index reindex job died at 03:42 UTC leaving the live index "
                    "empty. Service returns 200 OK with zero hits — no error in app "
                    "logs, but customers can't find anything. Reindex job restartable."
                ),
                suggested_fix={
                    "action": "restart_reindex_job",
                    "target": "search-service",
                    "params": "Restart the nightly-reindex Cloud Run job. ETA 6 minutes "
                              "to full index. Search will use cached top-50 categories "
                              "in the meantime so popular browse still works.",
                },
            ),
        }
        if kind not in recipes:
            raise ValueError(f"unknown incident kind: {kind}")
        inc = recipes[kind]
        with self._lock:
            self._incidents[inc.incident_id] = inc
        return inc

    def resolve_incident(self, incident_id: str) -> Optional[Incident]:
        with self._lock:
            inc = self._incidents.get(incident_id)
            if inc and not inc.resolved_at:
                inc.resolved_at = datetime.now(timezone.utc)
            return inc

    def clear_incidents(self) -> int:
        with self._lock:
            n = len(self._incidents)
            self._incidents.clear()
            return n

    # ---------- agent action log ----------

    def log_action(self, action: dict) -> None:
        with self._lock:
            self._action_log.insert(0, {**action, "at": datetime.now(timezone.utc).isoformat()})
            del self._action_log[200:]

    def recent_actions(self, n: int = 25) -> List[dict]:
        with self._lock:
            return list(self._action_log[:n])

    # ---------- human approvals ----------

    def stage_action(self, action: dict) -> str:
        action_id = f"ACT-{uuid.uuid4().hex[:6].upper()}"
        with self._lock:
            self._approved_actions[action_id] = {
                "action": action,
                "approved": False,
                "executed": False,
                "result": None,
                "staged_at": datetime.now(timezone.utc).isoformat(),
            }
        return action_id

    def approve_action(self, action_id: str) -> Optional[dict]:
        with self._lock:
            entry = self._approved_actions.get(action_id)
            if entry:
                entry["approved"] = True
            return entry

    def get_action(self, action_id: str) -> Optional[dict]:
        with self._lock:
            return self._approved_actions.get(action_id)

    def mark_executed(self, action_id: str, result: dict) -> None:
        with self._lock:
            entry = self._approved_actions.get(action_id)
            if entry:
                entry["executed"] = True
                entry["result"] = result
                entry["executed_at"] = datetime.now(timezone.utc).isoformat()


# Module-level singleton
store = StoreSimulator()
