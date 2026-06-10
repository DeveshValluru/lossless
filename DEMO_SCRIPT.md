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

> "Small online retailers lose about $2,300 every hour their checkout is broken — and most of them have no idea it's happening until their accountant tells them on Monday. The signals are sitting there in Dynatrace, but the store owner doesn't speak SRE. So I built **Lossless** — an AI agent that watches the store, translates incidents into dollars and customer journeys, and only acts when the owner says yes."

**Screen**: Brand header, then pan across the dashboard. Show the health grade (A), the green conversion funnel, all bars full. Clean state.

### 0:25 – 0:55 · Inject the incident

> "Friday night, peak hour. I'll simulate what we see all the time — the payment gateway starts timing out."

**Screen**: Click **"Trigger payment outage"**. Watch: the health grade drops from A to F. The "Revenue lost" tile turns red. The conversion funnel's last bar (Completing purchase) collapses — the bottleneck tag appears. The problem list updates.

> "Look at the funnel — customers can browse, they can add to cart, but they CAN'T check out. That's the retail insight: not just 'something is broken' but exactly where in the customer journey people are dropping off."

### 0:55 – 1:50 · Agent investigates and proposes

> "Let me ask the agent."

**Screen**: Type **"Where are customers dropping off and how do we fix it?"** and send.

**Voice over the tool calls**:
> "The agent calls `list_problems`, `analyze_conversion_funnel`, and `quantify_revenue_impact` — in parallel. It uses the conversion funnel to trace the bottleneck back to the payment service, then proposes a fix."

**Screen**: Agent replies in plain English — mentions the funnel bottleneck, the dollar impact, peak hours context, and proposes failover to Stripe backup.

> "Plain English. Customers-and-dollars, not p95s. And — critically — it stages the fix but REFUSES to execute until a human approves. That's built structurally: the `execute_remediation` tool checks for approval, not a prompt hack."

### 1:50 – 2:25 · Approve and verify

**Screen**: Click **"Approve & apply"**.

> "One click. The agent executes, then re-checks both service health AND the conversion funnel to confirm the customer journey is recovering."

**Screen**: Action log shows tool calls. The funnel bars fill back up. Health grade climbs back toward A. Revenue-lost tile stops climbing.

> "Done. We just resolved an incident a store owner could not have triaged alone — and they're back to selling."

### 2:25 – 2:50 · Architecture & impact

**Screen**: Brief flash of the architecture diagram from the README.

> "Under the hood: Gemini 3 on Vertex AI orchestrates a multi-step tool loop. The Dynatrace MCP server — the official `dynatrace-oss` package — is spawned as a subprocess. We layer on retail-specific tools: a conversion funnel analyzer, revenue-impact calculator, peak-hours awareness, and a staged remediation system with human approval gates. The agent thinks like a retail manager, not a DevOps engineer."

### 2:50 – 3:00 · Close

> "Lossless brings the visibility big retailers spend millions on to the corner store. Thank you."

**Screen**: Github URL + hosted URL on screen.

---

## Recording tips

- Pause for 1 sec after each click so viewers can see the cause→effect.
- If a take starts going long, stop at the next natural break and resume.
- Save the source recording; we'll keep the polished one for upload.
- Upload to YouTube as **Unlisted** (not Private — judges need to view).
