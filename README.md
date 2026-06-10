# Lossless — AI Store Ops Agent

> **Lossless** is an AI agent that lets non-technical retail managers monitor and fix their digital storefront in plain English. Built for the [Google Cloud Rapid Agent Hackathon 2026](https://rapid-agent.devpost.com/) on the **Dynatrace track**.

Brick-and-mortar retailers who run an online storefront on the side don't have an SRE team. When checkout breaks at 7pm on Friday, they don't see a Grafana alert — they see their Saturday revenue evaporate. Lossless watches their store through Dynatrace, translates incidents into dollars, proposes a fix in one sentence, and only acts when the owner says "yes."

---

## Why this matters

Small/mid retailers lose an estimated **$2,300 per hour** of online checkout downtime on a typical Saturday. The technical signals are already there in Dynatrace — the problem is that no one in the store is reading them. Lossless closes the loop: observability data → business impact → human approval → execution → verification.

---

## What it does

When you open the dashboard you see your store's live health at a glance: a letter grade (A through F), a conversion funnel showing where shoppers are dropping off, revenue lost in the last 30 minutes, and service-level latency and error rates. When something goes wrong you talk to the agent:

> **Manager:** "What's costing me money right now and how do we fix it?"
>
> **Lossless:** *(calls `list_problems`, `get_problem_details`, `quantify_revenue_impact`)*
> Your payments gateway is timing out — you're losing about **$1,840 in the last 30 minutes** because checkouts are failing. I can switch you to the Stripe backup processor; takes 45 seconds and no in-flight cart is affected. Want me to do it?
>
> **Manager:** "yes"
>
> **Lossless:** *(stages `propose_remediation` → manager clicks Approve → agent calls `execute_remediation`, then `get_service_health` to verify)*
> Done. Payments are back to normal latency (320 ms). Conversion is recovering.

The agent does multi-step tool use end-to-end and always asks for sign-off before applying a change. No surprises, no full-autonomy black box.

---

## Architecture

```
┌────────────────────────────────────────────────────────────────┐
│  Browser (vanilla JS + HTML/CSS)                               │
│  • Live KPI dashboard (revenue lost, conv rate, sessions)      │
│  • Chat panel + inline "Approve" buttons                       │
│  • Agent action log                                            │
└──────────────────────────┬─────────────────────────────────────┘
                           │  HTTP/JSON
┌──────────────────────────▼─────────────────────────────────────┐
│  FastAPI app  (app/main.py)                                    │
│  GET  /api/dashboard          live store snapshot              │
│  POST /api/chat               talk to the agent                │
│  POST /api/actions/{id}/approve  human sign-off                │
│  POST /api/demo/inject        scripted incident scenarios      │
└──────────────────────────┬─────────────────────────────────────┘
                           │
              ┌────────────┴────────────┐
              │                         │
┌─────────────▼─────────────┐  ┌────────▼────────────────────────┐
│  LosslessAgent (Gemini 3) │  │  StoreSimulator                 │
│  app/agent.py             │  │  app/store.py                   │
│  • Function calling loop  │  │  • Mock 4-service e-commerce    │
│  • System prompt: "be a   │  │  • Injectable incident recipes  │
│    store manager's AI"    │  │  • Revenue-impact math          │
│  • Retail-specific tools  │  └────────┬────────────────────────┘
└─────────────┬─────────────┘           │
              │                         │
              ▼                         ▼
┌──────────────────────────┐  ┌─────────────────────────────────┐
│  MCPBridge               │  │  TelemetryBackend               │
│  app/mcp_bridge.py       │  │  app/telemetry.py               │
│  • Spawns the official   │  │  • DynatraceBackend (REST API)  │
│    Dynatrace MCP server  │  │  • SyntheticBackend (store)     │
│  • Falls back to local   │  └────────┬────────────────────────┘
│    synthetic impls when  │           │
│    DT tenant is absent   │           ▼
└─────────────┬────────────┘  ┌──────────────────────────────────┐
              │               │  Dynatrace SaaS tenant           │
              ▼               │  (optional — synthetic mode      │
   ┌──────────────────────┐   │   works without it)              │
   │ Dynatrace MCP server │◄──┘                                  │
   │ npx @dynatrace-oss/  │                                      │
   │ dynatrace-mcp-server │                                      │
   │ (Node subprocess)    │                                      │
   └──────────────────────┘                                      │
```

### Key technical decisions

- **Google Cloud Gemini 3 Pro** via the Vertex AI Gen AI SDK (`google-genai`). Manual function-call loop so every step is captured in the action log.
- **Dynatrace MCP integration** is the official `@dynatrace-oss/dynatrace-mcp-server` Node binary, spawned via the Python `mcp` SDK over stdio. Tool names (`list_problems`, `get_problem_details`, `execute_dql`, `get_service_health`) match the upstream server so swapping in/out is transparent.
- **Graceful synthetic fallback.** If you don't have a Dynatrace trial yet, the bridge serves a Dynatrace-shaped response from the in-memory store, so reviewers can run the demo with zero external setup.
- **Human-in-the-loop by design.** `propose_remediation` always *stages* an action; `execute_remediation` refuses to run until the manager clicks Approve. This is the hackathon's "user oversight" requirement made structural, not a system-prompt suggestion.
- **Conversion funnel analysis.** The agent tracks the full shopping journey (visitors → product views → add-to-cart → checkout → purchase) and shows exactly where customers drop off per incident type. A payment outage collapses the purchase stage; a search outage kills product discovery. This is genuinely retail-specific intelligence, not generic DevOps with labels.
- **Peak-hours awareness.** The agent knows traffic patterns and factors them into impact estimates — the same outage costs 2x during evening peak vs. 3 AM.

---

## Running locally

```bash
# 1. Install dependencies
python -m pip install -r requirements.txt

# 2. Copy env file and fill in credentials
cp .env.example .env
#   Required:  GOOGLE_CLOUD_PROJECT (and `gcloud auth application-default login`)
#              OR GOOGLE_API_KEY
#   Optional:  DT_ENVIRONMENT + DT_PLATFORM_TOKEN (else synthetic mode)

# 3. Run
uvicorn app.main:app --reload --port 8080

# 4. Open http://localhost:8080
```

Click any "Trigger …" button in the bottom-left to inject a synthetic incident, then chat with the agent.

## Deploying to Cloud Run

```bash
gcloud run deploy lossless \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=$(gcloud config get-value project)" \
  --set-secrets "DT_PLATFORM_TOKEN=dt-platform-token:latest"
```

The Dockerfile bundles Node 20 so the Dynatrace MCP server can spawn from inside the container.

---

## Hackathon checklist

- [x] Built on **Google Cloud Agent Builder / Gemini 3** (Vertex AI Gen AI SDK)
- [x] Meaningful **Dynatrace MCP** integration (official `dynatrace-oss/dynatrace-mcp-server`)
- [x] Multi-step agent with tool use (detect → diagnose → quantify → propose → execute → verify)
- [x] **Brick-and-mortar retail** challenge domain
- [x] User-oversight built in (stage-then-approve flow)
- [x] Hosted URL · public repo · OSS-licensed (MIT)
- [x] 3-minute demo video

## Repo layout

```
.
├── app/
│   ├── agent.py        Gemini 3 agent with function calling
│   ├── mcp_bridge.py   Dynatrace MCP server bridge + synthetic fallback
│   ├── telemetry.py    Dynatrace REST API client (dashboard data)
│   ├── store.py        In-memory retail storefront + funnel + health score
│   ├── config.py       Settings
│   └── main.py         FastAPI app
├── static/
│   ├── index.html      Single-page UI
│   ├── styles.css
│   └── app.js
├── Dockerfile          Python 3.12 + Node 20 (for MCP server)
├── requirements.txt
├── .env.example
└── README.md
```

## License

MIT — see [LICENSE](./LICENSE).
