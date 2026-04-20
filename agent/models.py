"""
Pydantic data models for the Conversion Engine.
Covers: prospect data, signal briefs, ICP classification, email drafts,
conversation state, and trace records.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

# ── Enums ──────────────────────────────────────────────────────────────


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ICPSegment(StrEnum):
    RECENTLY_FUNDED = "segment_1_recently_funded"
    MID_MARKET_RESTRUCTURING = "segment_2_mid_market_restructuring"
    LEADERSHIP_TRANSITION = "segment_3_leadership_transition"
    CAPABILITY_GAP = "segment_4_capability_gap"
    ABSTAIN = "abstain"


class EmailType(StrEnum):
    COLD = "cold"
    WARM_REPLY = "warm_reply"
    RE_ENGAGEMENT = "re_engagement"


class SignalWeight(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ChannelType(StrEnum):
    EMAIL = "email"
    SMS = "sms"
    VOICE = "voice"


class ConversationStatus(StrEnum):
    NEW = "new"
    OUTBOUND_SENT = "outbound_sent"
    REPLIED = "replied"
    QUALIFIED = "qualified"
    CALL_BOOKED = "call_booked"
    STALLED = "stalled"
    OPTED_OUT = "opted_out"
    HANDED_OFF = "handed_off"


# ── Prospect & Firmographics ──────────────────────────────────────────


class ProspectInfo(BaseModel):
    """Core prospect identification."""

    company: str
    domain: str | None = None
    crunchbase_id: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    contact_title: str | None = None
    hq_location: str | None = None
    timezone: str | None = None
    employee_count: int | None = None
    industry: str | None = None
    description: str | None = None


# ── Signal Brief Components ───────────────────────────────────────────


class SourceRef(BaseModel):
    url: str | None = None
    description: str | None = None


class FundingSignal(BaseModel):
    event: str | None = None  # "Series A" | "Series B" | null
    amount_usd: int | None = None
    closed_at: str | None = None
    confidence: Confidence = Confidence.LOW
    sources: list[SourceRef] = Field(default_factory=list)


class HiringSignal(BaseModel):
    open_eng_roles: int | None = None
    ai_adjacent_eng_roles: int | None = None
    delta_60d: str | None = None  # e.g. "+18"
    confidence: Confidence = Confidence.LOW
    sources: list[SourceRef] = Field(default_factory=list)


class LayoffSignal(BaseModel):
    event: bool = False
    headcount_pct: float | None = None
    closed_at: str | None = None
    confidence: Confidence = Confidence.LOW
    sources: list[SourceRef] = Field(default_factory=list)


class LeadershipSignal(BaseModel):
    change: bool = False
    role: str | None = None
    name: str | None = None
    appointed_at: str | None = None
    confidence: Confidence = Confidence.LOW
    sources: list[SourceRef] = Field(default_factory=list)


class AIMaturityInput(BaseModel):
    type: str  # e.g. "ai_adjacent_roles", "named_leadership", etc.
    weight: SignalWeight
    evidence: str | None = None
    url: str | None = None


class AIMaturitySignal(BaseModel):
    score: int = 0  # 0-3
    confidence: Confidence = Confidence.LOW
    inputs: list[AIMaturityInput] = Field(default_factory=list)
    language_notes: str | None = None


class PitchGuidance(BaseModel):
    segment_4_viable: bool = False
    tone_for_segment_1: str | None = None  # "scale_existing" | "stand_up_first"
    language_notes: str | None = None


class BenchMatch(BaseModel):
    matched: bool = False
    gap: str | None = None
    thin: bool = False


class HiringSignalBrief(BaseModel):
    """Complete hiring signal brief for a prospect."""

    prospect: ProspectInfo
    enriched_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    funding: FundingSignal = Field(default_factory=FundingSignal)
    hiring: HiringSignal = Field(default_factory=HiringSignal)
    layoffs: LayoffSignal = Field(default_factory=LayoffSignal)
    leadership: LeadershipSignal = Field(default_factory=LeadershipSignal)
    ai_maturity: AIMaturitySignal = Field(default_factory=AIMaturitySignal)
    pitch_guidance: PitchGuidance = Field(default_factory=PitchGuidance)
    bench_match: BenchMatch = Field(default_factory=BenchMatch)
    requires_human_review: bool = False
    human_review_reason: str | None = None


# ── Competitor Gap Brief ──────────────────────────────────────────────


class CompetitorRecord(BaseModel):
    company: str
    ai_maturity: int = 0
    source_urls: list[str] = Field(default_factory=list)


class GapEntry(BaseModel):
    practice: str
    cohort_adoption: str  # e.g. "3 of 5 top-quartile peers"
    prospect_has_it: bool = False
    confidence: Confidence = Confidence.MEDIUM


class CompetitorGapBrief(BaseModel):
    """Competitor gap analysis for a prospect's sector."""

    prospect: ProspectInfo
    sector: str | None = None
    size_band: str | None = None
    cohort: list[CompetitorRecord] = Field(default_factory=list)
    prospect_position: dict = Field(default_factory=dict)  # {"percentile": 35, "rank": "7 of 10"}
    gaps: list[GapEntry] = Field(default_factory=list)
    generated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# ── ICP Classification ────────────────────────────────────────────────


class EvidenceItem(BaseModel):
    signal: str
    value: str
    weight: str  # "qualifying" | "supporting" | "disqualifying"


class ICPClassification(BaseModel):
    """ICP segment classification output."""

    prospect: ProspectInfo
    segment: ICPSegment
    secondary_segment: ICPSegment | None = None
    confidence: Confidence
    evidence: list[EvidenceItem] = Field(default_factory=list)
    disqualifiers_checked: list[str] = Field(default_factory=list)
    overlap_notes: str | None = None
    pitch_guidance_ref: PitchGuidance | None = None


# ── Email Draft ───────────────────────────────────────────────────────


class GroundedClaim(BaseModel):
    claim: str
    source_field: str
    confidence: Confidence


class ProposedTime(BaseModel):
    prospect_local: str
    utc: str


class EmailDraft(BaseModel):
    """Structured email output from the drafter."""

    thread_id: str
    email_type: EmailType
    subject: str
    body: str
    proposed_times: list[ProposedTime] = Field(default_factory=list)
    calcom_link: str | None = None
    grounded_claims: list[GroundedClaim] = Field(default_factory=list)
    handoff_to_human: bool = False
    handoff_reason: str | None = None
    tone_check_score: float | None = None
    draft_metadata: dict = Field(
        default_factory=lambda: {
            "generated_at": datetime.utcnow().isoformat(),
            "marked_draft": True,
        }
    )


# ── Conversation State ───────────────────────────────────────────────


class ConversationMessage(BaseModel):
    role: str  # "agent" | "prospect"
    channel: ChannelType
    content: str
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    metadata: dict = Field(default_factory=dict)


class ConversationState(BaseModel):
    """Full state of a conversation with a prospect."""

    thread_id: str
    prospect: ProspectInfo
    status: ConversationStatus = ConversationStatus.NEW
    channel: ChannelType = ChannelType.EMAIL
    messages: list[ConversationMessage] = Field(default_factory=list)
    signal_brief: HiringSignalBrief | None = None
    gap_brief: CompetitorGapBrief | None = None
    classification: ICPClassification | None = None
    hubspot_contact_id: str | None = None
    calcom_booking_id: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# ── Trace Records ─────────────────────────────────────────────────────


class TraceRecord(BaseModel):
    """Structured trace for the evidence graph."""

    trace_id: str
    event_type: (
        str  # "enrichment" | "classification" | "email_sent" | "sms_sent" | "booking" | "llm_call"
    )
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    prospect_company: str | None = None
    thread_id: str | None = None
    input_data: dict = Field(default_factory=dict)
    output_data: dict = Field(default_factory=dict)
    cost_usd: float | None = None
    latency_ms: float | None = None
    model: str | None = None
    success: bool = True
    error: str | None = None
