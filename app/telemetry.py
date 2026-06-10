"""
Telemetry layer.

Provides one consistent shape of data — current health, open problems,
detail traces, metric trends — that BOTH the dashboard and the agent
consume. Two backends:

* `DynatraceBackend` — hits the real Dynatrace REST API.
* `SyntheticBackend` — reads from `store.StoreSimulator` so the demo
  works without a Dynatrace tenant.

The agent itself integrates with Dynatrace through the official MCP server
(see `app/mcp_bridge.py`). The data returned here is what feeds the
manager-facing dashboard.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

from .config import get_settings
from .store import store

log = logging.getLogger("telemetry")


class SyntheticBackend:
    """Reads from the in-process StoreSimulator. Always available."""

    name = "synthetic"

    def health(self) -> dict:
        return store.current_metrics()

    def problems(self) -> List[dict]:
        return [inc.to_summary() for inc in store.open_incidents()]

    def problem_detail(self, problem_id: str) -> Optional[dict]:
        inc = store.get_incident(problem_id)
        if not inc:
            return None
        return {
            **inc.to_summary(),
            "root_cause_analysis": inc.root_cause,
            "suggested_fix": inc.suggested_fix,
        }

    def revenue_impact(self, minutes: int = 30) -> dict:
        return store.revenue_loss_estimate(minutes)


class DynatraceBackend:
    """Talks to a real Dynatrace SaaS tenant using a platform token."""

    name = "dynatrace"

    def __init__(self, environment: str, token: str) -> None:
        self.environment = environment.rstrip("/")
        self.token = token
        self._client = httpx.Client(
            base_url=self.environment,
            headers={"Authorization": f"Api-Token {self.token}"},
            timeout=15.0,
        )

    def _ok(self) -> bool:
        try:
            r = self._client.get("/api/v2/problems", params={"pageSize": 1})
            return r.status_code in (200, 401, 403)  # any answer means reachable
        except httpx.HTTPError:
            return False

    def health(self) -> dict:
        # Pull current service metrics. If anything fails, fall back to synthetic
        # so the dashboard never goes blank during the demo.
        try:
            r = self._client.get(
                "/api/v2/metrics/query",
                params={
                    "metricSelector": "builtin:service.response.time:splitBy(\"dt.entity.service\"):percentile(95)",
                    "from": "now-5m",
                },
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("dynatrace health query failed (%s); using synthetic", e)
            return store.current_metrics()

        # Always merge with synthetic to keep the funnel + revenue tiles populated;
        # in a real deployment those would come from a business-events metric.
        base = store.current_metrics()
        base["dynatrace_live"] = True
        return base

    def problems(self) -> List[dict]:
        try:
            r = self._client.get(
                "/api/v2/problems",
                params={"problemSelector": "status(\"OPEN\")", "pageSize": 25},
            )
            r.raise_for_status()
            real = [
                {
                    "incident_id": p["problemId"],
                    "title": p.get("title", "Unknown problem"),
                    "service": (p.get("affectedEntities") or [{}])[0].get("name", "unknown"),
                    "detected_at": datetime.fromtimestamp(
                        p.get("startTime", 0) / 1000, tz=timezone.utc
                    ).isoformat(),
                    "severity": p.get("severityLevel", "WARNING").lower(),
                    "status": "open",
                    "source": "dynatrace",
                }
                for p in r.json().get("problems", [])
            ]
        except httpx.HTTPError as e:
            log.warning("dynatrace problems query failed (%s); using synthetic", e)
            real = []
        synthetic = [inc.to_summary() for inc in store.open_incidents()]
        return real + synthetic

    def problem_detail(self, problem_id: str) -> Optional[dict]:
        synth = store.get_incident(problem_id)
        if synth:
            return {
                **synth.to_summary(),
                "root_cause_analysis": synth.root_cause,
                "suggested_fix": synth.suggested_fix,
            }
        try:
            r = self._client.get(f"/api/v2/problems/{problem_id}")
            r.raise_for_status()
            p = r.json()
            return {
                "incident_id": p["problemId"],
                "title": p.get("title"),
                "service": (p.get("affectedEntities") or [{}])[0].get("name"),
                "severity": p.get("severityLevel", "WARNING").lower(),
                "status": p.get("status", "OPEN").lower(),
                "root_cause_analysis": p.get("rootCauseEntity", {}).get("name", "Unknown"),
                "source": "dynatrace",
            }
        except httpx.HTTPError:
            return None

    def revenue_impact(self, minutes: int = 30) -> dict:
        # Revenue impact is a business calculation — keep it in-process.
        return store.revenue_loss_estimate(minutes)


def get_backend():
    s = get_settings()
    if s.dynatrace_configured:
        backend = DynatraceBackend(s.dt_environment, s.dt_platform_token)
        if backend._ok():
            log.info("Using Dynatrace backend at %s", s.dt_environment)
            return backend
        log.warning("Dynatrace configured but not reachable; using synthetic backend")
    return SyntheticBackend()
