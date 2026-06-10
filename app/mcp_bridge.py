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
import sys
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .config import get_settings
from .store import store

# On Windows, `npx` is `npx.cmd` and asyncio subprocess won't resolve it
# without the suffix. On POSIX it's just `npx`.
NPX_COMMAND = "npx.cmd" if sys.platform == "win32" else "npx"

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
            command=NPX_COMMAND,
            args=["-y", "@dynatrace-oss/dynatrace-mcp-server"],
            env={
                **os.environ,
                "DT_ENVIRONMENT": self.settings.dt_environment,
                "DT_PLATFORM_TOKEN": self.settings.dt_platform_token,
                "OAUTH_TOKEN": self.settings.dt_platform_token,
            },
        )
        log.info("Spawning Dynatrace MCP server: %s -y @dynatrace-oss/dynatrace-mcp-server",
                 NPX_COMMAND)
        try:
            self._stack = AsyncExitStack()
            read, write = await self._stack.enter_async_context(stdio_client(params))
            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            # First run downloads the npm package (can take ~60s on cold start)
            await asyncio.wait_for(self._session.initialize(), timeout=90.0)
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

    # Tools the real Dynatrace MCP server exposes. Everything else we serve
    # from the synthetic backend.
    REMOTE_TOOLS = {
        "get_environment_info",
        "list_vulnerabilities",
        "list_problems",
        "find_entity_by_name",
        "verify_dql",
        "execute_dql",
        "generate_dql_from_natural_language",
        "explain_dql_in_natural_language",
        "list_exceptions",
    }

    async def _call_remote_raw(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        res = await asyncio.wait_for(
            self._session.call_tool(name, arguments or {}),
            timeout=25.0,
        )
        text = ""
        for c in res.content:
            text += getattr(c, "text", "") or ""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Route a tool call to real Dynatrace MCP and/or our synthetic backend.

        * `list_problems` — call BOTH (so we surface real DT problems AND
          demo-injected synthetic incidents in a single list).
        * Other REMOTE_TOOLS — call real MCP, fall back to synthetic on error.
        * Anything else (our custom tools) — synthetic only.
        """
        async with self._lock:
            # list_problems: merge real + synthetic so injected demo incidents always show
            if name == "list_problems":
                synth = self._synthetic_call(name, arguments)
                synth_problems = synth.get("result") or []
                if self._connected and self._session:
                    try:
                        real = await self._call_remote_raw(name, arguments)
                        real_problems = real if isinstance(real, list) else (
                            real.get("problems", []) if isinstance(real, dict) else []
                        )
                        return {
                            "source": "dynatrace-mcp + synthetic",
                            "result": list(real_problems) + list(synth_problems),
                            "real_count": len(real_problems),
                            "synthetic_count": len(synth_problems),
                        }
                    except Exception as e:
                        log.warning("MCP list_problems failed (%s); synthetic only", e)
                return synth

            # Tools that exist on the real Dynatrace MCP
            if name in self.REMOTE_TOOLS and self._connected and self._session:
                try:
                    result = await self._call_remote_raw(name, arguments)
                    return {"source": "dynatrace-mcp", "result": result}
                except Exception as e:
                    log.warning("MCP %s failed (%s); falling back to synthetic", name, e)

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
        if name == "analyze_conversion_funnel":
            return {
                "source": "synthetic",
                "result": {
                    "funnel": store.conversion_funnel(),
                    "peak_hours": store.peak_hour_context(),
                    "health_grade": store.health_score(),
                },
            }
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
