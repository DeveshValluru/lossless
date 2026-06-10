# Lossless — 3-minute demo video script

Target run time: **2:50–3:00**. Speak quickly but not rushed. Total ~430 words.

## Pre-recording checklist

- [ ] Server running locally: `uvicorn app.main:app --port 8080`
- [ ] `.env` has `GOOGLE_API_KEY` (or `GOOGLE_CLOUD_PROJECT`) so the agent works
- [ ] Browser at `http://localhost:8080` zoomed to 110% for legibility
- [ ] Screen recorder (Loom or OBS) set to 1080p
- [ ] Hit "↺ Reset" before starting
- [ ] Close other tabs / Slack / notifications
- [ ] Sip water; warm up your voice

## Storyboard

### 0:00 – 0:25 · The pain (hook)

> "Small online retailers lose about $2,300 every hour their checkout is broken — and most of them have no idea it's happening until their accountant tells them on Monday. The signals are all sitting there in Dynatrace, but the store owner doesn't speak SRE. So I built **Lossless** — an AI agent that watches the store, translates incidents into dollars, and only acts when the owner says yes."

**Screen**: Brand header, then quick pan across the green dashboard. No incidents yet.

### 0:25 – 0:55 · Inject the incident

> "Friday night, peak hour. I'll simulate what we see all the time — the payment gateway starts timing out."

**Screen**: Click **"💳 Trigger payment outage"**. The "Revenue lost" tile turns red and starts climbing. Conversion rate drops. A new problem appears.

> "Within seconds, the store is hemorrhaging. The dashboard catches it, but a non-technical owner still wouldn't know what to do."

### 0:55 – 1:50 · Agent investigates and proposes

> "Let me just ask the agent what's going on."

**Screen**: Type **"What's costing me money right now and how do we fix it?"** and send.

**Voice over the tool calls**:
> "Notice it's calling `list_problems`, `get_problem_details`, and our `quantify_revenue_impact` tool — these are real Dynatrace MCP tool names. The agent is using the same MCP protocol we'd use in production."

**Screen**: Agent replies — something like *"Your payment gateway is timing out — you've lost about $87 in the last two minutes. I can fail over to the Stripe backup processor in 45 seconds, no in-flight cart affected. Want me to do it?"*

> "Plain English. Dollars, not p95s. And — critically — it's asking permission. The hackathon brief emphasised user oversight, so we built it structurally: the `execute_remediation` tool refuses to run until a human approves."

### 1:50 – 2:25 · Approve and verify

**Screen**: Click **"Approve & apply"**.

> "One click. The agent now calls `execute_remediation`, watches `get_service_health` come back green, and reports the recovery."

**Screen**: Action log shows: `execute_remediation`, `get_service_health`. The revenue-lost tile freezes; the problem clears.

> "Done. We just resolved an incident a store owner could not have triaged alone — and they're back to selling."

### 2:25 – 2:50 · Architecture & impact

**Screen**: Brief flash of the architecture diagram from the README.

> "Under the hood: Gemini 3 Pro on Vertex AI orchestrates a multi-step tool loop. The Dynatrace MCP server — the official `dynatrace-oss/dynatrace-mcp-server` — is spawned as a subprocess, with a synthetic-mode fallback so judges can run the demo with zero external setup. The agent layers Dynatrace's observability tools with retail-specific ones we wrote: revenue-impact math, staged remediations, and verified rollouts."

### 2:50 – 3:00 · Close

> "Lossless brings the visibility big retailers spend millions on to the corner store. Thank you."

**Screen**: Github URL + hosted URL on screen.

---

## Recording tips

- Pause for 1 sec after each click so viewers can see the cause→effect.
- If a take starts going long, stop at the next natural break and resume.
- Save the source recording; we'll keep the polished one for upload.
- Upload to YouTube as **Unlisted** (not Private — judges need to view).
