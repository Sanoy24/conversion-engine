"""
Full-thread end-to-end demo for Act II deliverable:
  one complete email + SMS + calendar thread for a synthetic prospect.

Flow (all stages emit traces to outputs/full_thread_traces.jsonl):
  1. Enrich + classify + draft cold email
  2. Send email (Resend; kill-switch → staff sink unless LIVE_OUTBOUND_ENABLED=1)
  3. Simulate inbound reply (no real webhook needed for the demo)
  4. Draft warm reply
  5. Send SMS scheduling ping (Africa's Talking; kill-switch applies)
  6. Create HubSpot contact with enrichment fields + timestamp
  7. Book Cal.com discovery-call slot (30 min, +24h)
  8. Write outputs/full_thread_trace.json with per-stage timing + status

Integration calls (HubSpot, Cal.com) that lack real credentials degrade
gracefully: the stage is marked `ok=False` with the error captured in the
trace, and the demo continues so later stages can be observed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from agent.channels.email_handler import send_email
from agent.channels.sms_handler import send_sms
from agent.config import settings
from agent.core.orchestrator import handle_prospect_reply, process_new_prospect
from agent.integrations.calcom import get_calcom_client
from agent.integrations.hubspot import get_hubspot_client
from agent.models import EmailDraft, ProspectInfo

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"

logger = logging.getLogger("full_thread_demo")


def _stage_record(name: str, start: float, ok: bool, **extras) -> dict:
    return {
        "stage": name,
        "ok": ok,
        "latency_ms": round((time.monotonic() - start) * 1000, 1),
        "timestamp": datetime.utcnow().isoformat(),
        **extras,
    }


async def run(company_name: str, contact_name: str, contact_email: str,
              contact_phone: str, contact_title: str) -> dict:
    OUTPUTS.mkdir(exist_ok=True)
    traces_path = OUTPUTS / "full_thread_traces.jsonl"
    # Per-run summary is overwritten; aggregate traces are appended across runs
    # so 20 prospect runs produce a 20-run latency sample for the report.
    result_path = OUTPUTS / "full_thread_trace.json"

    stages: list[dict] = []
    thread_id: str | None = None

    def _emit(stage: dict) -> None:
        stages.append(stage)
        with traces_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(stage) + "\n")
        logger.info("stage=%s ok=%s latency=%.0fms",
                    stage["stage"], stage["ok"], stage["latency_ms"])

    # ── 1. Enrich + classify + draft cold email ─────────────────────────
    t0 = time.monotonic()
    pipeline = await process_new_prospect(
        company_name=company_name,
        contact_name=contact_name,
        contact_email=contact_email,
        contact_title=contact_title,
    )
    thread_id = pipeline["thread_id"]
    _emit(_stage_record(
        "1_enrich_classify_draft", t0, True,
        thread_id=thread_id,
        segment=pipeline["classification"]["segment"],
        confidence=pipeline["classification"]["confidence"],
        gap_brief_present=pipeline.get("gap_brief") is not None,
        pipeline_latency_ms=pipeline.get("pipeline_latency_ms"),
        total_cost_usd=pipeline.get("total_cost_usd"),
        email_subject=pipeline["email_draft"]["subject"],
    ))

    draft_dict = pipeline["email_draft"]
    draft = EmailDraft.model_validate(draft_dict)
    prospect = ProspectInfo.model_validate(pipeline["prospect"])
    prospect.contact_phone = prospect.contact_phone or contact_phone

    # ── 2. Send cold email ──────────────────────────────────────────────
    t0 = time.monotonic()
    email_resp, email_trace = await send_email(
        to_email=prospect.contact_email or contact_email,
        draft=draft,
    )
    _emit(_stage_record(
        "2_email_send", t0, email_trace.success,
        resend_status=email_resp.get("status"),
        resend_id=email_resp.get("id"),
        kill_switch_active=not settings.live_outbound_enabled,
        trace_id=email_trace.trace_id,
    ))

    # ── 3. Simulate inbound reply ───────────────────────────────────────
    t0 = time.monotonic()
    reply_text = (
        f"Hi — thanks for reaching out. We're scaling the data platform team "
        f"right now; can you share a 30-min window this week? "
        f"My mobile for scheduling: {contact_phone}. Best, {contact_name}"
    )
    reply_out = await handle_prospect_reply(thread_id=thread_id, reply_content=reply_text)
    _emit(_stage_record(
        "3_reply_received_and_warm_draft", t0, True,
        action=reply_out.get("action"),
        requires_handoff=reply_out.get("handoff_to_human", False),
        reply_chars=len(reply_text),
    ))

    # ── 4. Send SMS scheduling ping ─────────────────────────────────────
    t0 = time.monotonic()
    sms_text = (
        f"Hi {contact_name.split()[0]} — following up on your reply to my email. "
        f"Would Thu 14:00 UTC or Fri 10:00 UTC work for a 30-min chat? Reply 1 or 2."
    )
    sms_resp, sms_trace = await send_sms(
        to_phone=prospect.contact_phone or contact_phone,
        message=sms_text,
        thread_id=thread_id,
    )
    _emit(_stage_record(
        "4_sms_scheduling", t0, sms_trace.success,
        at_status=sms_resp.get("status"),
        kill_switch_active=not settings.live_outbound_enabled,
        trace_id=sms_trace.trace_id,
    ))

    # ── 5. Create HubSpot contact ───────────────────────────────────────
    t0 = time.monotonic()
    hubspot = get_hubspot_client()
    hs_resp, hs_trace = await hubspot.create_contact(
        prospect=prospect,
        signal_brief=None,  # full object is stored in the conversation; HS gets
        classification=None,  # the summary note separately below.
    )
    hs_contact_id = hs_resp.get("id") if isinstance(hs_resp, dict) else None
    _emit(_stage_record(
        "5_hubspot_contact_created", t0, hs_trace.success,
        hubspot_contact_id=hs_contact_id,
        error=hs_resp.get("error") if isinstance(hs_resp, dict) else None,
        trace_id=hs_trace.trace_id,
    ))

    # Add a note with the thread summary (best-effort)
    if hs_contact_id:
        t0 = time.monotonic()
        note_body = (
            f"Conversion Engine thread {thread_id}\n"
            f"Segment: {pipeline['classification']['segment']} "
            f"(confidence: {pipeline['classification']['confidence']})\n"
            f"AI maturity: {pipeline['signal_brief']['ai_maturity']['score']}/3\n"
            f"Cold subject: {draft.subject}\n"
            f"Reply received at: {datetime.utcnow().isoformat()}"
        )
        note_resp, note_trace = await hubspot.add_note(
            contact_id=hs_contact_id,
            note_body=note_body,
            prospect_company=prospect.company,
        )
        _emit(_stage_record(
            "5b_hubspot_note", t0, note_trace.success,
            note_id=note_resp.get("id") if isinstance(note_resp, dict) else None,
            trace_id=note_trace.trace_id,
        ))

    # ── 6. Book Cal.com discovery call ──────────────────────────────────
    t0 = time.monotonic()
    calcom = get_calcom_client()
    # Try candidate slots spread across future days/hours until Cal.com accepts one.
    # We have 1 calendar owner across the whole demo, so each 30-min slot can
    # only take one prospect. Generate a wide pool (many days × many hours) so
    # 20+ back-to-back runs don't exhaust it.
    #
    # Policy: bookings must land AFTER the challenge week ends (Sat Apr 25,
    # 2026, 21:00 UTC). We anchor the floor at Mon May 4, 2026 — 9 days past
    # the deadline — so no demo booking can fall inside a staff member's
    # challenge-week calendar, even if someone re-runs this before today.
    CHALLENGE_END = datetime(2026, 4, 25, 21, 0, 0)
    BOOKING_FLOOR = datetime(2026, 5, 4, 0, 0, 0)  # Mon after challenge week
    today_floor = datetime.utcnow() + timedelta(days=14)
    booking_base = max(today_floor, BOOKING_FLOOR)

    cal_resp, cal_trace = None, None
    _candidate_offsets: list[tuple[int, int, int]] = []
    for _days in range(0, 42):                           # ~6 weeks starting from the floor
        for _hour in (9, 10, 11, 13, 14, 15, 16):        # 7 business-hour slots
            for _minute in (0, 30):                      # two half-hour starts
                _candidate_offsets.append((_days, _hour, _minute))
    # Deterministic shuffle per-run so different prospects try different slots
    # first (keeps the failure-then-retry log noise short on average).
    import random as _random
    _random.Random(thread_id).shuffle(_candidate_offsets)

    booked = False
    for _days, _hour, _minute in _candidate_offsets:
        _fb = (booking_base + timedelta(days=_days)).replace(
            hour=_hour, minute=_minute, second=0, microsecond=0)
        # Hard safety check: never propose a slot during the challenge week.
        if _fb <= CHALLENGE_END:
            continue
        start = _fb.isoformat() + "Z"
        end = (_fb + timedelta(minutes=30)).isoformat() + "Z"
        cal_resp, cal_trace = await calcom.create_booking(
            prospect=prospect,
            start_time=start,
            end_time=end,
            notes=(f"Discovery call — segment={pipeline['classification']['segment']}, "
                   f"thread={thread_id}"),
        )
        if cal_trace.success:
            booked = True
            break  # booked successfully — stop trying
    _booking_id = (cal_resp.get("id") or cal_resp.get("uid")
                   if isinstance(cal_resp, dict) else None)
    _emit(_stage_record(
        "6_calcom_booking", t0, cal_trace.success,
        booking_id=_booking_id,
        start_time=start,
        error=cal_resp.get("error") if isinstance(cal_resp, dict) else None,
        trace_id=cal_trace.trace_id,
    ))

    # ── 7. Link Cal.com booking → HubSpot update (rubric 3.3) ──────────
    # A completed Cal.com booking triggers a corresponding HubSpot record
    # update: lead status → CALL_BOOKED plus an engagement note with the
    # booking metadata. Skipped if the booking failed or we don't have a
    # HubSpot contact id to attach to.
    if cal_trace.success and hs_contact_id:
        t0 = time.monotonic()
        try:
            await hubspot.update_contact_status(
                contact_id=hs_contact_id,
                status="CALL_BOOKED",
                properties={"hs_lead_status": "CALL_BOOKED"},
            )
            booking_note = (
                f"Discovery call booked via Cal.com.\n"
                f"Booking ID: {_booking_id}\n"
                f"Scheduled: {start}\n"
                f"Segment: {pipeline['classification']['segment']}\n"
                f"Thread: {thread_id}"
            )
            _, _link_note_trace = await hubspot.add_note(
                contact_id=hs_contact_id,
                note_body=booking_note,
                prospect_company=prospect.company,
            )
            _emit(_stage_record(
                "7_calcom_to_hubspot_link", t0, _link_note_trace.success,
                hubspot_contact_id=hs_contact_id,
                booking_id=_booking_id,
                trace_id=_link_note_trace.trace_id,
            ))
        except Exception as e:
            logger.error("Cal.com → HubSpot link failed: %s", e)
            _emit(_stage_record(
                "7_calcom_to_hubspot_link", t0, False,
                error=str(e),
                hubspot_contact_id=hs_contact_id,
            ))

    summary = {
        "run_id": f"full_thread_{uuid.uuid4().hex[:8]}",
        "thread_id": thread_id,
        "kill_switch_active": not settings.live_outbound_enabled,
        "prospect": {
            "company": prospect.company,
            "contact_name": contact_name,
            "contact_email": contact_email,
            "contact_phone": contact_phone,
        },
        "classification": pipeline["classification"]["segment"],
        "pipeline_total_cost_usd": pipeline.get("total_cost_usd"),
        "stages": stages,
        "ok": all(s["ok"] for s in stages),
        "total_latency_ms": round(sum(s["latency_ms"] for s in stages), 1),
    }
    result_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(json.dumps({
        "thread_id": thread_id,
        "ok": summary["ok"],
        "stages_ok": [s["stage"] for s in stages if s["ok"]],
        "stages_failed": [s["stage"] for s in stages if not s["ok"]],
        "total_latency_ms": summary["total_latency_ms"],
    }, indent=2))

    # Tear down the HubSpot client from the same task that created it.
    # This avoids anyio's "cancel scope exited in a different task" warning
    # when a long-lived MCP stdio session gets garbage-collected at interpreter
    # shutdown. No-op for the direct-API client.
    try:
        await get_hubspot_client().close()
    except Exception as e:
        logger.warning("HubSpot client close failed: %s", e)
    # Reset module singletons so a fresh session is created on the next run
    # (when this script is invoked as part of a multi-run batch).
    import agent.integrations.hubspot as _hs_mod
    _hs_mod._hubspot = None
    _hs_mod._hubspot_client_impl = None
    try:
        import agent.integrations.hubspot_mcp as _hs_mcp_mod
        _hs_mcp_mod._hubspot_mcp = None
    except ImportError:
        pass

    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", default="Consolety")
    ap.add_argument("--contact-name", default="Alex Demo")
    ap.add_argument("--contact-email", default="demo-prospect@example.com")
    ap.add_argument("--contact-phone", default="+254700000000")
    ap.add_argument("--contact-title", default="CTO")
    a = ap.parse_args()
    asyncio.run(run(a.company, a.contact_name, a.contact_email,
                    a.contact_phone, a.contact_title))
