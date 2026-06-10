"""
LosslessAgent — Gemini 3 + Dynatrace MCP + retail-specific tools.

The agent's job is to be a store-operations co-pilot for a non-technical
brick-and-mortar retail manager. It reasons over observability data
(Dynatrace MCP), translates incidents into dollars-lost, proposes
remediations, asks for human approval, then executes and verifies.

Design notes:
* Tool calls flow through `MCPBridge.call_tool()` which abstracts over
  real Dynatrace MCP and the synthetic backend.
* We run Gemini's function-call loop manually (no `automatic_function_calling`)
  so we can log every step for the UI's action log.
* A "propose_remediation" call STAGES an action; "execute_remediation" only
  runs once a human has approved it. This is the user-oversight requirement
  the hackathon rules explicitly call out.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import get_settings
from .mcp_bridge import DYNATRACE_TOOL_SCHEMAS, mcp_bridge
from .store import store

log = logging.getLogger("agent")

SYSTEM_PROMPT = """\
You are **Lossless**, an AI Store Operations agent for a brick-and-mortar retail
business that also sells online. You report to a NON-TECHNICAL store manager
— they think in customers, dollars, and shopping journeys, not in p95 latency.

🔧 MANDATORY TOOL USAGE 🔧
You MUST use tools to answer EVERY question about the store. You have NO direct
knowledge of the store's current state — every number you cite MUST come from a
tool call. Inventing or guessing metrics is a critical failure.

TOOL ROUTING (follow exactly):
* "how's the store" / status / health → call `list_problems` + `get_service_health` + `analyze_conversion_funnel` (all 3 in parallel)
* money / revenue / impact / cost → also call `quantify_revenue_impact`
* fix / remediate / resolve → call `get_problem_details` then `propose_remediation`
* approved / yes / go ahead → call `execute_remediation` then `get_service_health`
* where are customers dropping off / funnel → call `analyze_conversion_funnel`

Call multiple tools IN PARALLEL in the SAME response when they're independent.

🛒 RETAIL REASONING FRAMEWORK 🛒
You think like a retail operations expert, not a DevOps engineer:

1. **Customer Journey First**: Always frame issues in terms of the shopping
   funnel — are customers unable to find products? Unable to add to cart?
   Unable to check out? Each stage has different revenue impact.

2. **Peak Hours Awareness**: The conversion funnel tool tells you if this is
   peak shopping time. An outage during lunch rush or evening peak costs 2-3x
   more than the same outage at 3 AM. Always mention this.

3. **Revenue Translation**: Every technical metric must be converted to dollars.
   Don't say "error rate is 27%". Say "roughly 3 out of 10 customers trying
   to pay are getting errors — that's costing you about $X per minute."

4. **Bottleneck Identification**: Use the funnel to identify WHERE customers
   are dropping off, then trace back to which service is causing it. This is
   your unique value — the manager doesn't know that "search-service errors"
   means "customers can't find products and leave."

Workflow for a degraded storefront:
1. Check the funnel to see WHERE customers are dropping off
2. Translate technical problem → business impact in 1-2 plain sentences
   ("Customers can find products but can't check out. You're losing ~$40/min.")
3. IMMEDIATELY call `propose_remediation` to STAGE the fix — don't ask first.
   Staging is harmless; it does NOT execute.
4. In your reply, summarise the staged fix and ask the manager to approve.
5. Only call `execute_remediation` after they explicitly say yes/approve.
6. After executing, call `get_service_health` + `analyze_conversion_funnel`
   to confirm the funnel is recovering, then report.

Tone: warm, calm, terse. Treat the manager like a smart business owner who has
ten other things to do. Surface the numbers that matter and skip the jargon.
When you give a $ figure, say what time window it covers. When you describe an
impact, say which part of the customer journey is affected.
"""

# Retail-specific tools layered on top of the Dynatrace MCP toolset.
RETAIL_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "quantify_revenue_impact",
        "description": (
            "Estimate revenue lost over a recent window due to current degradations. "
            "Returns expected vs actual revenue and a $ loss figure."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "window_minutes": {
                    "type": "integer",
                    "description": "How many minutes back to compute the loss over (5-120).",
                    "default": 30,
                }
            },
        },
    },
    {
        "name": "propose_remediation",
        "description": (
            "Generate a concrete remediation plan for an open problem and STAGE it "
            "for human approval. Returns an action_id and human-readable summary. "
            "Does NOT execute. The manager must approve before you call "
            "execute_remediation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "problem_id": {"type": "string", "description": "Problem id to fix."}
            },
            "required": ["problem_id"],
        },
    },
    {
        "name": "execute_remediation",
        "description": (
            "Execute a previously-staged remediation. ONLY call this after the "
            "manager has explicitly said yes/approve/go ahead. Returns the "
            "outcome of the action."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action_id": {"type": "string", "description": "Staged action id."}
            },
            "required": ["action_id"],
        },
    },
    {
        "name": "get_action_status",
        "description": "Check whether a staged action is approved and executed.",
        "parameters": {
            "type": "object",
            "properties": {
                "action_id": {"type": "string"}
            },
            "required": ["action_id"],
        },
    },
    {
        "name": "analyze_conversion_funnel",
        "description": (
            "Analyze the customer shopping funnel: visitors → product views → "
            "add-to-cart → checkout → purchase. Shows WHERE in the journey "
            "customers are dropping off, identifies the bottleneck stage, and "
            "includes peak-hours context. Essential for understanding revenue "
            "impact of any incident."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
]

ALL_TOOL_SCHEMAS = DYNATRACE_TOOL_SCHEMAS + RETAIL_TOOL_SCHEMAS


@dataclass
class AgentTurn:
    user_message: str
    final_text: str = ""
    tool_calls: List[dict] = field(default_factory=list)
    proposed_action_id: Optional[str] = None
    mode: str = "synthetic"


class LosslessAgent:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._client = None
        self._history: List[Any] = []  # google.genai Content list

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        from google import genai
        from google.genai import types

        # Tighten the SDK's internal retry so a single 429 doesn't burn 4 quota
        # units. Our own retry logic in _generate_with_retry takes over.
        http_options = types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1))

        mode = self.settings.gemini_auth_mode
        if mode == "vertex":
            self._client = genai.Client(
                vertexai=True,
                project=self.settings.google_cloud_project,
                location=self.settings.google_cloud_location,
                http_options=http_options,
            )
            log.info("Gemini client: Vertex AI (%s/%s)",
                     self.settings.google_cloud_project,
                     self.settings.google_cloud_location)
        elif mode == "api_key":
            self._client = genai.Client(
                api_key=self.settings.google_api_key,
                http_options=http_options,
            )
            log.info("Gemini client: AI Studio API key")
        else:
            raise RuntimeError(
                "Gemini is not configured. Set GOOGLE_API_KEY (AI Studio) "
                "or GOOGLE_CLOUD_PROJECT (Vertex AI)."
            )
        return self._client

    def _gemini_tool_config(self):
        from google.genai import types

        decls = []
        for s in ALL_TOOL_SCHEMAS:
            decls.append(
                types.FunctionDeclaration(
                    name=s["name"],
                    description=s["description"],
                    parameters=s["parameters"],
                )
            )
        return [types.Tool(function_declarations=decls)]

    async def _execute_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        log.info("tool call: %s(%s)", name, json.dumps(args)[:200])

        # Retail tools are handled in-process.
        if name == "quantify_revenue_impact":
            window = int(args.get("window_minutes", 30))
            window = max(5, min(window, 120))
            return store.revenue_loss_estimate(window)

        if name == "propose_remediation":
            pid = args.get("problem_id", "")
            inc = store.get_incident(pid)
            if not inc:
                # Maybe a real Dynatrace problem id — pull details from MCP
                detail = await mcp_bridge.call_tool("get_problem_details", {"problem_id": pid})
                result = detail.get("result")
                if not result:
                    return {"error": f"problem {pid} not found"}
                suggested = result.get("suggested_fix", {
                    "action": "investigate_manually",
                    "target": result.get("service", "unknown"),
                    "params": "No automated remediation available; escalate to engineer.",
                })
                title = result.get("title", "issue")
            else:
                suggested = inc.suggested_fix
                title = inc.title

            action_id = store.stage_action({
                "problem_id": pid,
                "title": title,
                "plan": suggested,
            })
            return {
                "action_id": action_id,
                "summary": suggested.get("params", ""),
                "target_service": suggested.get("target", ""),
                "approval_required": True,
                "ui_hint": "Show an approve button to the manager.",
            }

        if name == "execute_remediation":
            action_id = args.get("action_id", "")
            entry = store.get_action(action_id)
            if not entry:
                return {"error": f"action {action_id} not found"}
            if not entry["approved"]:
                return {
                    "error": "not_approved",
                    "message": "The manager has not approved this action yet. "
                               "Wait for confirmation before retrying.",
                }
            # "Execute" the fix in our mock store: resolve the related incident
            plan = entry["action"]["plan"]
            pid = entry["action"]["problem_id"]
            resolved = store.resolve_incident(pid)
            result = {
                "executed_action": plan.get("action"),
                "target": plan.get("target"),
                "applied_change": plan.get("params"),
                "incident_resolved": bool(resolved and resolved.resolved_at),
                "executed_at_iso": (resolved.resolved_at.isoformat()
                                   if resolved and resolved.resolved_at else None),
            }
            store.mark_executed(action_id, result)
            return result

        if name == "get_action_status":
            action_id = args.get("action_id", "")
            entry = store.get_action(action_id)
            if not entry:
                return {"error": f"action {action_id} not found"}
            return {
                "action_id": action_id,
                "approved": entry["approved"],
                "executed": entry["executed"],
                "result": entry.get("result"),
            }

        if name == "analyze_conversion_funnel":
            funnel = store.conversion_funnel()
            peak = store.peak_hour_context()
            health = store.health_score()
            return {
                "funnel": funnel,
                "peak_hours": peak,
                "health_grade": health,
            }

        # Everything else goes through the Dynatrace MCP bridge.
        return await mcp_bridge.call_tool(name, args)

    async def chat(self, user_message: str) -> AgentTurn:
        client = self._ensure_client()
        from google.genai import types

        turn = AgentTurn(user_message=user_message, mode=mcp_bridge.mode)

        # Build history with the new user turn appended.
        self._history.append(
            types.Content(role="user", parts=[types.Part(text=user_message)])
        )

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=self._gemini_tool_config(),
            temperature=0.4,
        )

        max_steps = 8
        model_state = {"name": self.settings.gemini_model}
        step = 0
        while step < max_steps:
            response = await self._generate_with_retry(client, model_state, config)

            # Append model's response content (parts incl. function calls / text)
            candidate = response.candidates[0] if response.candidates else None
            if candidate and candidate.content:
                self._history.append(candidate.content)

            # Collect function calls if any
            function_calls = []
            for part in (candidate.content.parts if candidate and candidate.content else []):
                fc = getattr(part, "function_call", None)
                if fc and fc.name:
                    function_calls.append(fc)

            if not function_calls:
                turn.final_text = (response.text or "").strip()
                break

            # Execute all function calls and append their responses
            response_parts = []
            for fc in function_calls:
                args = dict(fc.args) if fc.args else {}
                tool_result = await self._execute_tool(fc.name, args)
                turn.tool_calls.append({
                    "name": fc.name,
                    "args": args,
                    "result_preview": _truncate(tool_result, 400),
                })
                store.log_action({
                    "kind": "tool_call",
                    "name": fc.name,
                    "args": args,
                    "result_preview": _truncate(tool_result, 400),
                })
                # capture proposed action id for the frontend
                if fc.name == "propose_remediation" and isinstance(tool_result, dict):
                    turn.proposed_action_id = tool_result.get("action_id")
                response_parts.append(
                    types.Part.from_function_response(name=fc.name, response={"result": tool_result})
                )

            self._history.append(types.Content(role="user", parts=response_parts))
            step += 1

        if not turn.final_text:
            turn.final_text = (
                "I ran out of steps mid-investigation — could you re-ask, or give me "
                "a hint about which area to focus on?"
            )

        store.log_action({
            "kind": "chat_turn",
            "user": user_message,
            "final": turn.final_text[:500],
            "tool_call_count": len(turn.tool_calls),
            "mode": turn.mode,
        })
        return turn

    async def _generate_with_retry(self, client, model_state: dict, config):
        """Call Gemini, handling one model-fallback and up to N rate-limit retries.

        `model_state["name"]` may be mutated to the fallback model on the first
        failure of the primary, so subsequent calls in this turn use the
        already-resolved working model.
        """
        primary_model = self.settings.gemini_model
        fallback_model = self.settings.gemini_fallback_model
        rate_limit_retries = 0
        max_rate_limit_retries = 2

        while True:
            try:
                return await asyncio.to_thread(
                    client.models.generate_content,
                    model=model_state["name"],
                    contents=self._history,
                    config=config,
                )
            except Exception as e:
                err = str(e).lower()
                rate_limited = ("429" in err or "resource_exhausted" in err
                                or "quota" in err)
                model_missing = ("404" in err or "not found" in err
                                 or "permission" in err or "invalid" in err)

                if (model_state["name"] == primary_model
                        and fallback_model
                        and fallback_model != primary_model
                        and (rate_limited or model_missing)):
                    log.warning("Model %s failed (%s); switching to %s",
                                model_state["name"], str(e)[:140], fallback_model)
                    model_state["name"] = fallback_model
                    continue

                if rate_limited and rate_limit_retries < max_rate_limit_retries:
                    rate_limit_retries += 1
                    wait = 15.0 * rate_limit_retries
                    log.warning(
                        "Rate-limited on %s; sleeping %.0fs then retrying (%d/%d)",
                        model_state["name"], wait,
                        rate_limit_retries, max_rate_limit_retries,
                    )
                    await asyncio.sleep(wait)
                    continue
                raise

    def reset(self) -> None:
        self._history = []


def _truncate(obj: Any, n: int) -> Any:
    try:
        s = json.dumps(obj, default=str)
    except Exception:
        s = str(obj)
    return s[:n] + ("…" if len(s) > n else "")


# Module-level singleton
agent = LosslessAgent()
