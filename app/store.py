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

# Conversion funnel baseline rates (each as % of total visitors)
FUNNEL_BASELINE = {
    "visitors":          1.0,
    "product_views":     0.68,
    "add_to_cart":       0.24,
    "checkout_started":  0.14,
    "purchase_complete": 0.034,
}

FUNNEL_LABELS = {
    "visitors":          "Browsing the store",
    "product_views":     "Viewing products",
    "add_to_cart":       "Adding to cart",
    "checkout_started":  "Starting checkout",
    "purchase_complete": "Completing purchase",
}

# Peak-hours traffic multipliers (hour-of-day UTC → multiplier)
# Modelled on a US retailer whose customers peak at lunch + evening
PEAK_HOURS = {
    0: 0.3, 1: 0.2, 2: 0.15, 3: 0.1, 4: 0.1, 5: 0.15,
    6: 0.3, 7: 0.5, 8: 0.7, 9: 0.85, 10: 0.95, 11: 1.1,
    12: 1.3, 13: 1.35, 14: 1.2, 15: 1.0, 16: 1.05, 17: 1.25,
    18: 1.4, 19: 1.35, 20: 1.2, 21: 1.0, 22: 0.7, 23: 0.45,
}


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

    # ---------- conversion funnel ----------

    def conversion_funnel(self) -> dict:
        """Conversion funnel with incident-aware degradation at each stage.

        This is the retail-specific insight: instead of just "error rate is high",
        we show WHERE in the shopping journey customers are dropping off.
        """
        with self._lock:
            visitors = int(820 + 80 * math.sin(time.time() / 120))
            current = dict(FUNNEL_BASELINE)

            for inc in self._incidents.values():
                if inc.resolved_at:
                    continue
                if inc.service == "search-service":
                    # Search broken → customers can't find products
                    current["product_views"] *= 0.15
                    current["add_to_cart"] *= 0.15
                    current["checkout_started"] *= 0.15
                    current["purchase_complete"] *= 0.15
                elif inc.service == "cart-service":
                    # Cart slow → add-to-cart and downstream drop
                    current["add_to_cart"] *= 0.45
                    current["checkout_started"] *= 0.45
                    current["purchase_complete"] *= 0.45
                elif inc.service == "payments-service":
                    # Payment failures → checkout/purchase collapse
                    current["purchase_complete"] *= 0.32
                elif inc.service == "web-frontend":
                    # Frontend issues → everyone is affected
                    for k in current:
                        if k != "visitors":
                            current[k] *= 0.40

            stages = []
            for name in FUNNEL_BASELINE:
                base_pct = FUNNEL_BASELINE[name]
                cur_pct = current[name]
                drop = round((1 - cur_pct / base_pct) * 100, 1) if base_pct > 0 else 0
                stages.append({
                    "stage": name,
                    "label": FUNNEL_LABELS[name],
                    "baseline_count": int(visitors * base_pct),
                    "current_count": int(visitors * cur_pct),
                    "baseline_pct": round(base_pct * 100, 1),
                    "current_pct": round(cur_pct * 100, 1),
                    "drop_pct": max(0, drop),
                })

            # Find the bottleneck — the stage with the worst drop
            worst = max(stages, key=lambda s: s["drop_pct"])
            bottleneck = worst["stage"] if worst["drop_pct"] > 5 else None

            return {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "total_visitors": visitors,
                "stages": stages,
                "bottleneck": bottleneck,
                "bottleneck_label": FUNNEL_LABELS.get(bottleneck, ""),
                "bottleneck_drop_pct": worst["drop_pct"] if bottleneck else 0,
            }

    # ---------- health score ----------

    def health_score(self) -> dict:
        """Overall store health as an A-F grade. Non-technical managers get this."""
        metrics = self.current_metrics()
        incidents = self.open_incidents()

        score = 100
        reasons: List[str] = []

        # Incidents
        for inc in incidents:
            if inc.severity == "critical":
                score -= 30
                reasons.append(f"Critical: {inc.title}")
            elif inc.severity == "warning":
                score -= 15
                reasons.append(f"Warning: {inc.title}")

        # Conversion degradation
        conv_ratio = (metrics["conversion_rate"]
                      / metrics["baseline_conversion_rate"])
        if conv_ratio < 0.5:
            score -= 20
            reasons.append(f"Conversion down {round((1 - conv_ratio) * 100)}%")
        elif conv_ratio < 0.8:
            score -= 10
            reasons.append(f"Conversion down {round((1 - conv_ratio) * 100)}%")

        # Service-level degradation
        for svc, data in metrics["services"].items():
            if data["latency_ms_p95"] > 2000:
                score -= 10
                reasons.append(f"{svc}: latency {data['latency_ms_p95']}ms")
            if data["error_rate"] > 0.10:
                score -= 10
                reasons.append(f"{svc}: {round(data['error_rate'] * 100, 1)}% errors")

        score = max(0, min(100, score))
        if score >= 90:
            grade, label = "A", "Excellent"
        elif score >= 75:
            grade, label = "B", "Good"
        elif score >= 60:
            grade, label = "C", "Degraded"
        elif score >= 40:
            grade, label = "D", "Poor"
        else:
            grade, label = "F", "Critical"

        return {"score": score, "grade": grade, "label": label, "reasons": reasons}

    # ---------- peak hours ----------

    def peak_hour_context(self) -> dict:
        """What's the traffic multiplier right now, and is it a bad time for an outage?"""
        now = datetime.now(timezone.utc)
        hour = now.hour
        multiplier = PEAK_HOURS.get(hour, 1.0)
        if multiplier >= 1.2:
            period = "peak shopping hours"
            severity_boost = "Impact is amplified — traffic is {:.0f}% above normal.".format(
                (multiplier - 1) * 100
            )
        elif multiplier >= 0.8:
            period = "normal hours"
            severity_boost = ""
        else:
            period = "off-peak hours"
            severity_boost = "Silver lining: traffic is low right now, so fewer customers affected."

        return {
            "hour_utc": hour,
            "period": period,
            "traffic_multiplier": multiplier,
            "context": severity_boost,
        }

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
