---
name: tenacious-voice-rig
description: Integrate with the Tenacious Shared Voice Rig for the bonus-tier end-to-end voice demo — webhook registration, keyword prefix routing, one real discovery-call booking through voice. Use this skill whenever the user mentions the voice rig, the Shared Voice Rig, voice agent, τ²-Bench voice ceiling (~42%), voice fallback, voice pass@1, or the 8-minute demo video's bonus segment about a real voice call end-to-end. Voice is the final channel for Tenacious — a discovery call booked by the agent and delivered by a human — not a cold-outreach channel. Cold voice to founders/CTOs burns the contact. This skill is strictly for warm-lead scheduling handoff or the demo, not for cold outbound.
---

# Tenacious Voice Rig Integration

The bonus-tier channel. Deliberately narrow scope: voice is not a cold-outreach surface for Tenacious prospects. Founders, CTOs, and VPs Engineering live in email. A cold voice call to this segment is unusual and frequently perceived as intrusive — attempting it at scale would damage the brand more than skipping voice entirely.

This skill is for two specific situations:

1. **Warm-lead scheduling handoff.** A prospect has replied by email, indicated urgency, moved to SMS for coordination, and asked for a quick voice confirmation of the discovery call time. The agent makes one short call, confirms, disconnects.
2. **The 8-minute demo video's bonus segment.** One end-to-end voice call through the Shared Voice Rig showing the full loop works. Attempted only if the core demo is already solid.

The challenge explicitly warns: a trainee who rebuilds a voice-heavy architecture here will find their prospects do not respond to voice cold outreach. Matching channel to segment is graded.

## The Shared Voice Rig — what it is

The program operates a shared voice rig. Each trainee gets a registered webhook URL + a keyword prefix that routes calls to their handler. The rig is the inbound/outbound voice surface; your code is the conversation logic.

Things you get from the rig:
- A phone number (shared across trainees, disambiguated by keyword prefix).
- Webhook URLs for call events (`call.started`, `speech.recognized`, `call.ended`).
- A speech-to-text transcript per turn.
- A text-to-speech endpoint for your agent's response.

Things you are responsible for:
- Registering your webhook correctly on Day 0.
- Handling webhook events within the rig's timeout (usually 5s for speech response).
- Routing your keyword prefix correctly so the rig's multiplexer sends calls to your handler.
- Graceful degradation if the rig is unreachable — the call falls back to a "please confirm by SMS or email" message.

## Day 0 provisioning

On the Day 0 readiness review, this skill's prerequisite is a registered webhook with a test call routed to your handler. If that check did not pass, do not attempt voice in the challenge week — the debugging eats into core deliverables.

Verify:
- Webhook URL is HTTPS and publicly reachable.
- Keyword prefix is unique to you (registered with program staff).
- A test call to the shared number with your prefix reaches your handler with a populated transcript.
- Your handler's TTS response is audible in the test call.

## The two conversation shapes

### Shape 1 — Warm-lead scheduling confirmation (30–90 seconds)

Trigger: prospect has replied by email and SMS, scheduling is pending, prospect signals urgency.

Conversation arc:
- Agent identifies itself: "Hi, this is a voice confirmation from Tenacious — we have a 30-minute discovery call proposed for [day] at [time prospect-local]. Does that still work?"
- If yes: "Great, I will send a calendar invite to [email]. Anything else before the call?"
- If no: "No problem. Could you reply to the email thread or SMS with a time that works better? I do not want to rebook over voice to avoid miscommunication."
- Disconnect politely.

The call is confirmation, not negotiation. Do not attempt to reschedule over voice — the failure modes (misheard times, ambiguous time zones) are too expensive. The agent falls back to email/SMS for any substantive scheduling change.

### Shape 2 — Demo segment (≤ 2 minutes)

For the demo video:
- A synthetic prospect (staff-controlled sink) calls the rig with the trainee's keyword prefix.
- The agent picks up, confirms a pre-arranged discovery call time, books it through Cal.com, disconnects.
- The recording shows: call audio + Cal.com booking populating + HubSpot contact updating in real time.

This is a scripted happy path. The point is to show the loop works end-to-end, not to exercise edge cases. Edge cases live in the probe library, not the demo reel.

## Conversation constraints

These are Tenacious-voice specific, not generic voice-agent rules:

- **No pricing over voice.** The pricing sheet allows public-tier quotes by email; voice compresses nuance and a misquoted rate is a brand risk. If asked: "I would not want to quote rates on a call where we cannot both see the numbers — I will send the public bands by email immediately after this call."
- **No capacity commitments.** Same rule as the email drafter: do not promise specific engineers or start dates. The bench summary is text-based ground truth; voice is a bad surface for reading from it in real time.
- **No case study names.** The three redacted case studies in the seed repo may be referenced by sector + size only. Misspeaking a client name on a call is unrecoverable.
- **No AI-generated claims about the prospect.** If the signal brief says `ai_maturity: 2, confidence: low`, do not assert AI maturity on the call. Voice pressure makes agents over-claim; enforce the honesty rule in the prompt explicitly.
- **TTS voice selection.** Pick a neutral professional voice. Do not use a voice that mimics a specific real person. Record which voice was selected in the trace so the memo can cite it.

## The τ²-Bench voice ceiling

Published reference: ~42% pass@1 on τ²-Bench retail for conversational voice agents (Feb 2026 leaderboard). This is the floor of the voice problem, and the bonus segment does not need to beat it — it needs to demonstrate the pipeline works. The memo's Skeptic's Appendix should acknowledge: voice adds a failure mode (misrecognition) on top of the text agent's failure modes, so real deployment would need additional probes specifically on transcript-error handling.

If the demo segment succeeds, add one paragraph to the appendix:
- What voice adds (fast warm-lead confirmation, channel-match signal).
- What voice risks (misrecognition, TTS uncanny-valley, prospect annoyance at unexpected call).
- Why Tenacious should not extend voice beyond confirmation until a voice-specific probe library is built.

## Failure handling

### Call connects but transcript is empty or garbled

Three-second pause, then: "I am having trouble hearing — would you mind replying to the email thread instead? I want to make sure we get the time right." Disconnect.

### Prospect asks a question outside the confirmation scope

Any question that is not yes/no about the proposed time: "Good question — let me come back to you in the email thread so we have it in writing." Disconnect.

### Rig webhook fails mid-call

Fall back to TTS "I seem to have lost connection — please reply to the email and I will confirm there." Disconnect.

### Kill-switch

If the rig is misbehaving (calls dropping, transcripts corrupting, latency > 5s consistently), the trainee-level kill-switch is: unregister the webhook. The rig falls back to a generic "please use email or SMS" message for any inbound calls to your prefix.

## What NOT to build

- **Cold outbound voice to any prospect.** Not for the demo, not for probes. The channel-match rule is graded; building cold-voice capability signals misreading the challenge.
- **Voice for the signal brief or competitor gap brief.** These are written artifacts. Reading them aloud is unnecessary and error-prone.
- **Complex conversation trees.** The voice conversation is confirmation-only. Every branch is "yes → book, no → handoff, unclear → handoff."
- **TTS voice cloning of any real person.** Policy violation.

## Integration points

- **Input:** the prospect's current state from HubSpot (proposed time, email thread ID, current calendar slot). Voice is the last channel in a sequence, never the first.
- **Output:** a call trace (audio + transcript + events) written to Langfuse, a Cal.com booking update, a HubSpot contact update with `voice_confirmed_at` timestamp.
- **Handoff back:** if voice fails, the agent returns to email/SMS with no loss of state. The email thread ID is preserved.

## Cost envelope

Voice calls are cheap per-minute on the shared rig (program-covered). The cost to watch is developer time: getting a TTS response back within the rig's 5s timeout, with correct prosody, is the kind of bug that eats a day. Budget it honestly. If Day 6 afternoon is approaching and voice still does not work reliably, cut the bonus segment from the demo video and spend the time on memo polish.

## Never do this

- Never cold-call a prospect who has not already engaged by email and SMS.
- Never quote pricing, commit capacity, or reference client names on a voice call.
- Never clone a real person's voice for TTS.
- Never attempt scheduling negotiation over voice — confirm or handoff, never rebook.
- Never ship the voice segment if the core demo is incomplete.
