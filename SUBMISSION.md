# Devpost submission text

Copy-paste this into the Devpost form when you submit.

## Project name

Lossless — AI Store Ops Agent

## Tagline (~120 chars)

An AI agent that lets non-technical retail managers monitor and fix their digital storefront in plain English, before it costs them sales.

## Partner track

**Dynatrace**

## Challenge domain

Brick-and-mortar retail

## Inspiration

Small and mid-size retailers run more of their business online every year — but they don't have an SRE team. When checkout breaks at 7pm on Friday, they don't see a Grafana alert; they see Saturday's revenue evaporate on Monday morning when the accountant flags it. The technical signal was sitting in Dynatrace the whole time, but nobody who could read it was watching.

We built Lossless to close that loop. The store owner doesn't have to learn observability — the agent does.

## What it does

Lossless is an AI Store Operations agent that:

- Watches the digital storefront through the **official Dynatrace MCP server**.
- Translates technical incidents into business impact (dollars lost, customers affected, time window).
- Proposes a concrete, single-sentence remediation.
- **Only acts after the manager approves it** (staged action → human sign-off → execute → verify).
- Reports the outcome in plain English.

Example: when the payment gateway times out, the manager just asks "what's costing me money?" Lossless calls `list_problems` and `get_problem_details` over MCP, computes a revenue-loss estimate, proposes a failover to a backup processor, asks for approval, executes on approval, and confirms the storefront has recovered.

## How we built it

- **Google Cloud Gemini 3 Pro** on Vertex AI, accessed through the Gen AI SDK with a manual function-call loop (no automatic FC) so every tool call is captured for the manager-facing action log.
- **Dynatrace MCP** integration via the official `@dynatrace-oss/dynatrace-mcp-server` Node binary, spawned as a subprocess and talked to over stdio using the Python `mcp` client.
- A graceful synthetic fallback in the same module: when no Dynatrace tenant is configured, the bridge serves Dynatrace-shaped responses from an in-memory mock retail storefront so the demo never blanks out.
- **FastAPI** backend, single-file vanilla-JS frontend with a polished dark UI, deployed to **Cloud Run**.
- Layered our own retail-specific tools on top of Dynatrace's: `quantify_revenue_impact`, `propose_remediation` (stages action with approval gate), `execute_remediation` (refuses without approval), `get_action_status`.

## Challenges we ran into

- Designing a tool API where "human approval" is a structural guarantee, not a soft prompt suggestion the model can ignore.
- Making the demo robust when judges may not have a Dynatrace tenant: we built a tool-name-compatible synthetic adapter behind the same `MCPBridge` interface.
- Getting honest revenue-impact math: instead of always assuming a full 30-minute degraded window, the loss tile scales with time-since-detection, so a freshly-injected incident shows $40, not $13,000.

## Accomplishments we're proud of

- A 6-tool agent that genuinely executes end-to-end multi-step workflows on the Dynatrace MCP.
- An interface a real store owner could use without training.
- Built and submitted within the hackathon window with zero pre-existing code.

## What we learned

- The cleanest "user oversight" pattern in agent UX is two paired tools (`propose`/`execute`) that gate by id, not a prompt instruction.
- MCP makes the partner integration story click — once the bridge was working, adding new tools was a one-line change.

## What's next

- Real PoS integrations (Shopify, Square) so the same agent covers in-store + online ops.
- A nightly digest: "yesterday in the store" in three sentences and one chart.
- Multi-store rollout for franchisees.

## Built with

Google Cloud · Vertex AI · Gemini 3 Pro · Dynatrace · Dynatrace MCP Server · Model Context Protocol · Python · FastAPI · vanilla JS · Cloud Run

## Try it out (links)

- Live demo: <FILL IN AT DEPLOY TIME>
- Repository: <FILL IN AT GITHUB PUSH TIME>
- Demo video: <FILL IN AT UPLOAD TIME>
