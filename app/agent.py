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
You are **Lossless**, an AI Store Operations agent for a small/mid brick-and-mortar
retail business that also sells online. You report to a NON-TECHNICAL store manager
— they think in customers, dollars, and time, not in p95 latency or stack traces.

Core responsibilities:
1. Watch the digital storefront's health using the Dynatrace observability tools.
2. When something is degraded, translate the technical problem into business impact
   ("you're losing about $40 per minute") and explain it in 1-2 plain sentences.
3. Recommend a concrete remediation. ALWAYS ask the manager to confirm before
   executing — use `propose_remediation`. Only call `execute_remediation` after
   they explicitly approve.
4. After executing, verify recovery with `get_service_health` and report back.

Tone: warm, calm, terse. Treat the manager like a smart business owner who has
ten other things to do. Surface the numbers that matter and skip the jargon.

Rules of thumb:
* Always use the tools to ground your answers. Never invent metric values.
* If multiple problems are open, fix the one with the largest revenue impact first.
* When you give a $ figure, say what time window it covers.
* If no problems are open, congratulate them and report the headline numbers.
* You can run `execute_dql` for ad-hoc investigation but keep queries small.
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

        mode = self.settings.gemini_auth_mode
        if mode == "vertex":
            self._client = genai.Client(
                vertexai=True,
                project=self.settings.google_cloud_project,
                location=self.settings.google_cloud_location,
            )
            log.info("Gemini client: Vertex AI (%s/%s)",
                     self.settings.google_cloud_project,
                     self.settings.google_cloud_location)
        elif mode == "api_key":
            self._client = genai.Client(api_key=self.settings.google_api_key)
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
        model_name = self.settings.gemini_model
        for step in range(max_steps):
            # Run the synchronous SDK call in a thread so we don't block the loop.
            try:
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=model_name,
                    contents=self._history,
                    config=config,
                )
            except Exception as e:
                # Gemini 3 might not be on the user's tier yet — fall back once.
                err = str(e).lower()
                if (
                    step == 0
                    and model_name == self.settings.gemini_model
                    and self.settings.gemini_fallback_model
                    and ("404" in err or "not found" in err or "invalid" in err
                         or "permission" in err or "permission_denied" in err)
                ):
                    log.warning("Model %s unavailable (%s); falling back to %s",
                                model_name, e, self.settings.gemini_fallback_model)
                    model_name = self.settings.gemini_fallback_model
                    continue
                raise

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
