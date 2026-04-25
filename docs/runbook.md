# Tenacious Conversion Engine — Operational Runbook

This runbook is intended for the operator managing the Conversion Engine in a production or live-pilot environment. It details standard operating procedures (SOPs), incident response guidelines, and kill-switch management.

## 1. Core Operating Principles

1. **Safety First**: The kill-switch (`LIVE_OUTBOUND_ENABLED=false`) is the default state. Never flip it to `true` without written approval from the executive team for the specific prospect cohort being targeted.
2. **Brand Protection**: The tone-preservation check and AI-maturity confidence scores are guardrails. If they trigger frequently, it indicates the LLM is drifting or signal data is poor. Pause outbound and investigate.
3. **Observability is Mandatory**: No system operates without Langfuse tracing active. If Langfuse is unreachable, the system must either fall back to local JSONL logs successfully or fail-closed.

## 2. Kill-Switch Operations

### Enabling Live Outbound
To transition from dry-run to live pilot:
1. Ensure all test contacts have been purged from HubSpot.
2. Verify Resend domain is out of sandbox mode and Africa's Talking shortcode is provisioned.
3. Verify Cal.com routing points to the correct human delivery lead's calendar.
4. Set `LIVE_OUTBOUND_ENABLED=true` in the Render environment variables.
5. Deploy.
6. **Verification**: Send one test email to an internal Tenacious domain and confirm receipt.

### Emergency Shutdown (The "Red Button")
If an incident occurs (e.g., brand-damaging email sent, infinite loop, rogue bookings):
1. Navigate to the Render dashboard.
2. Update the environment variable `LIVE_OUTBOUND_ENABLED=false`.
3. Trigger a manual deploy to force the container to restart immediately.
4. Verify the logs show `event_type=routed_to_sink`.

## 3. Incident Response and Troubleshooting

### 3.1 Langfuse Degradation
**Symptoms**: High latency on LLM calls; `LangfuseError` in logs; dashboard unreachable.
**Impact**: Loss of live observability.
**Response**: 
- The system uses a dual-write mechanism. If Langfuse fails, local JSONL traces still capture the data.
- Do not stop the pipeline unless local traces also fail.

### 3.2 HubSpot MCP Connection Failure
**Symptoms**: Logs show `Failed to connect to HubSpot MCP server. Falling back to direct REST API.`
**Impact**: Minor. The system falls back to the REST API gracefully.
**Response**: 
- Check if `node` or `npx` was uninstalled or updated on the host.
- Verify the `HUBSPOT_ACCESS_TOKEN` is still valid and has not expired.

### 3.3 Playwright Scraper IP Blocks
**Symptoms**: Job-post velocity consistently returning `0` or `Confidence.LOW` across many prospects; logs show `PlaywrightTimeoutError` or 403 Forbidden.
**Impact**: Segment 1 classification drops drastically; AI maturity scoring loses a key input.
**Response**:
- The system handles this gracefully by substituting `Confidence.LOW` and falling back to "ASK" phrasing.
- To fix, integrate a residential proxy network (e.g., BrightData) into the Playwright launch options.

### 3.4 DeepSeek/OpenRouter Rate Limiting
**Symptoms**: 429 Too Many Requests errors from OpenRouter.
**Impact**: Pipeline stalls.
**Response**:
- Switch the `DEV_MODEL` variable to a fallback model (e.g., `openai/gpt-4o-mini`) via environment variables.

## 4. Daily Operations Checklist

- [ ] **Review Stalled Threads**: Check HubSpot for leads in `Stalled` status. Validate that the 14-day re-engagement sequence fired correctly.
- [ ] **Audit Low-Confidence Signals**: Grep traces for `Confidence.LOW` on AI maturity. If >30% of prospects trigger this, review the Playwright scraper health.
- [ ] **Review Tone Check Logs**: Search Langfuse for the tag `tone_check_failed`. If the LLM is consistently drifting from the style guide, adjust the system prompt temperature down from 0.7 to 0.4.
