"""
Bridge to the official Dynatrace MCP server (`@dynatrace-oss/dynatrace-mcp-server`).

When a Dynatrace tenant is configured (DT_ENVIRONMENT + DT_PLATFORM_TOKEN),
this module spawns the real MCP server over stdio and forwards agent tool
calls to it. When it isn't, we expose the same tool names with synthetic
implementations powered by `store.StoreSimulator` so the demo always works.

The agent code only ever talks to `MCPBridge.call_tool(name, args)` —
it doesn't need to know which mode is active.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .config import get_settings
from .store import store

log = logging.getLogger("mcp_bridge")

# Subset of Dynatrace MCP tool names we expose to the agent. Names match the
# real server so the integration is drop-in.
DYNATRACE_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "list_problems",
        "description": (
            "List currently open observability problems detected on the storefront "
            "(latency spikes, error-rate jumps, outages). Returns id, title, "
            "affected service, severity, and detection time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Limit on number of problems to return.",
                    "default": 25,
                }
            },
        },
    },
    {
        "name": "get_problem_details",
        "description": (
            "Fetch the full root-cause analysis, symptoms, and Dynatrace-suggested "
            "remediation for one problem id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "problem_id": {
                    "type": "string",
                    "description": "The problem / incident id, e.g. INC-AB12CD.",
                }
            },
            "required": ["problem_id"],
        },
    },
    {
        "name": "execute_dql",
        "description": (
            "Run a Dynatrace Query Language (DQL) query against Grail and return "
            "the result rows. Use this for ad-hoc investigation, e.g. counting "
            "error logs by service, summarising latency, or correlating events. "
            "Keep queries small (limit 50)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A valid DQL query string.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_service_health",
        "description": (
            "Get a snapshot of the storefront services' current latency, error "
            "rate and request volume."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
]


class MCPBridge:
    """Owns the lifecycle of the Dynatrace MCP server subprocess."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._stack: Optional[AsyncExitStack] = None
        self._session = None  # mcp.ClientSession when connected
        self._connected = False
        self._lock = asyncio.Lock()

    @property
    def mode(self) -> str:
        return "dynatrace-mcp" if self._connected else "synthetic"

    async def start(self) -> None:
        """Spawn the Dynatrace MCP server if a tenant is configured."""
        if not self.settings.dynatrace_configured:
            log.info("MCP bridge: Dynatrace not configured, running in synthetic mode")
            return
        try:
            # Lazy import so we don't crash if `mcp` isn't installed during tests
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except Exception as e:
            log.warning("mcp package unavailable (%s); synthetic mode only", e)
            return

        params = StdioServerParameters(
            command="npx",
            args=["-y", "@dynatrace-oss/dynatrace-mcp-server"],
            env={
                **os.environ,
                "DT_ENVIRONMENT": self.settings.dt_environment,
                "DT_PLATFORM_TOKEN": self.settings.dt_platform_token,
                "OAUTH_TOKEN": self.settings.dt_platform_token,
            },
        )
        try:
            self._stack = AsyncExitStack()
            read, write = await self._stack.enter_async_context(stdio_client(params))
            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(self._session.initialize(), timeout=20.0)
            self._connected = True
            log.info("Dynatrace MCP server connected (mode=%s)", self.mode)
        except Exception as e:
            log.warning("Failed to start Dynatrace MCP server (%s); synthetic mode", e)
            if self._stack:
                await self._stack.aclose()
            self._stack = None
            self._session = None
            self._connected = False

    async def stop(self) -> None:
        if self._stack:
            try:
                await self._stack.aclose()
            except Exception as e:
                log.debug("MCP stack close error: %s", e)
        self._connected = False
        self._session = None

    async def list_remote_tools(self) -> List[str]:
        if not self._connected or not self._session:
            return []
        try:
            res = await self._session.list_tools()
            return [t.name for t in res.tools]
        except Exception as e:
            log.warning("MCP list_tools failed: %s", e)
            return []

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Route a tool call: real Dynatrace MCP if connected, else synthetic."""
        async with self._lock:
            if self._connected and self._session:
                try:
                    res = await asyncio.wait_for(
                        self._session.call_tool(name, arguments or {}),
                        timeout=20.0,
                    )
                    text = ""
                    for c in res.content:
                        text += getattr(c, "text", "") or ""
                    try:
                        return {"source": "dynatrace-mcp", "result": json.loads(text)}
                    except json.JSONDecodeError:
                        return {"source": "dynatrace-mcp", "result": text}
                except Exception as e:
                    log.warning("MCP call_tool(%s) failed (%s); falling back", name, e)
            # Synthetic fallback
            return self._synthetic_call(name, arguments)

    # ---------- synthetic implementations ----------

    def _synthetic_call(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if name == "list_problems":
            max_results = int((args or {}).get("max_results", 25))
            return {
                "source": "synthetic",
                "result": [inc.to_summary() for inc in store.open_incidents()][:max_results],
            }
        if name == "get_problem_details":
            pid = (args or {}).get("problem_id", "")
            inc = store.get_incident(pid)
            if not inc:
                return {"source": "synthetic", "result": None, "error": f"no problem {pid}"}
            return {
                "source": "synthetic",
                "result": {
                    **inc.to_summary(),
                    "root_cause_analysis": inc.root_cause,
                    "suggested_fix": inc.suggested_fix,
                    "supporting_evidence": self._evidence_for(inc.service),
                },
            }
        if name == "execute_dql":
            return {"source": "synthetic", "result": self._fake_dql((args or {}).get("query", ""))}
        if name == "get_service_health":
            return {"source": "synthetic", "result": store.current_metrics()}
        return {"source": "synthetic", "result": None, "error": f"unknown tool {name}"}

    def _evidence_for(self, service: str) -> List[dict]:
        """Trace evidence we'd surface from Grail in real life."""
        now = datetime.now(timezone.utc).isoformat()
        if service == "payments-service":
            return [
                {"timestamp": now, "event": "HTTP 504 from PayGate-US-East", "count_5m": 412},
                {"timestamp": now, "event": "p95 authorize latency 2.7s (baseline 320ms)", "count_5m": 1},
                {"timestamp": now, "event": "circuit breaker tripped on payment-gateway-pool", "count_5m": 1},
            ]
        if service == "cart-service":
            return [
                {"timestamp": now, "event": "DB pool wait_time_ms p95 = 1180", "count_5m": 1},
                {"timestamp": now, "event": "cart-add latency 1.34s (baseline 240ms)", "count_5m": 1},
                {"timestamp": now, "event": "active_connections == pool_size (10/10)", "count_5m": 612},
            ]
        if service == "search-service":
            return [
                {"timestamp": now, "event": "search returned 0 hits for top 25 queries", "count_5m": 184},
                {"timestamp": now, "event": "reindex job last_run terminated 03:42 UTC", "count_5m": 1},
            ]
        return []

    def _fake_dql(self, query: str) -> dict:
        """A tiny stub so the agent's DQL queries return plausible data."""
        q = (query or "").lower()
        if "error" in q or "504" in q:
            return {
                "columns": ["service", "status_code", "count"],
                "rows": [
                    ["payments-service", "504", 412],
                    ["payments-service", "200", 891],
                    ["cart-service", "200", 4120],
                ],
            }
        if "latency" in q or "response.time" in q:
            return {
                "columns": ["service", "p95_ms"],
                "rows": [
                    ["payments-service", 2720],
                    ["cart-service", 240],
                    ["web-frontend", 195],
                    ["search-service", 90],
                ],
            }
        return {
            "columns": ["info"],
            "rows": [["DQL executed (demo). Configure DT_PLATFORM_TOKEN for real results."]],
        }


# Singleton — initialised by FastAPI lifespan
mcp_bridge = MCPBridge()
