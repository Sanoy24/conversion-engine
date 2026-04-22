# Conversion Engine

**An Automated Lead Generation and Conversion System for Tenacious Consulting and Outsourcing**

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    CONVERSION ENGINE                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
│  │  Crunchbase   │    │ layoffs.fyi  │    │  Job Posts   │      │
│  │  ODM Sample   │    │  CSV Parser  │    │  Playwright  │      │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘      │
│         │                   │                    │               │
│         └───────────┬───────┴────────────┬──────┘               │
│                     ▼                    ▼                       │
│  ┌─────────────────────────────────────────────────────┐       │
│  │            ENRICHMENT PIPELINE                       │       │
│  │  Funding · Hiring · Layoffs · Leadership · AI Mat.  │       │
│  │  → hiring_signal_brief.json                          │       │
│  │  → competitor_gap_brief.json                         │       │
│  └────────────────────┬────────────────────────────────┘       │
│                       ▼                                         │
│  ┌─────────────────────────────────────────────────────┐       │
│  │            ICP CLASSIFIER (with abstention)          │       │
│  │  Segment 1: Recently Funded                          │       │
│  │  Segment 2: Mid-Market Restructuring                 │       │
│  │  Segment 3: Leadership Transition                    │       │
│  │  Segment 4: Capability Gap                           │       │
│  └────────────────────┬────────────────────────────────┘       │
│                       ▼                                         │
│  ┌─────────────────────────────────────────────────────┐       │
│  │            EMAIL DRAFTER                              │       │
│  │  Confidence-aware phrasing (ASK/ASSERT)              │       │
│  │  Bench-gated commitments                              │       │
│  │  Tone-preservation check (style_guide.md)            │       │
│  │  Competitor gap hook (cold outbound)                  │       │
│  └────────────────────┬────────────────────────────────┘       │
│                       ▼                                         │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐               │
│  │   Resend    │  │  Africa's  │  │  Cal.com   │               │
│  │   Email     │  │  Talking   │  │  Calendar  │               │
│  │  (primary)  │  │   SMS      │  │  Booking   │               │
│  └──────┬─────┘  └──────┬─────┘  └──────┬─────┘               │
│         │               │               │                       │
│         └───────┬───────┴───────────────┘                       │
│                 ▼                                                │
│  ┌─────────────────────────────────────────────────────┐       │
│  │            HubSpot CRM                               │       │
│  │  Contact records · Enrichment timestamps             │       │
│  │  Conversation history · Lead status                  │       │
│  └─────────────────────────────────────────────────────┘       │
│                                                                 │
│  ┌────────────────┐  ┌────────────────┐                        │
│  │  Langfuse       │  │  Trace Logger  │                        │
│  │  Observability  │  │  (JSONL)       │                        │
│  └────────────────┘  └────────────────┘                        │
│                                                                 │
│  ┌─────────────────────────────────────────────────────┐       │
│  │            τ²-Bench Evaluation Harness               │       │
│  │  Dev slice (30 tasks) · Held-out (20 tasks)          │       │
│  │  5-trial pass@1 · 95% CI · Cost tracking             │       │
│  └─────────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────┘
```

## Project Structure

```
conversion-engine/
├── agent/                        # Core agent source files
│   ├── __init__.py
│   ├── config.py                 # Centralized configuration (env vars)
│   ├── models.py                 # Pydantic data models
│   ├── llm.py                    # LLM client (OpenRouter)
│   ├── main.py                   # FastAPI app + webhook endpoints
│   ├── channels/                 # Communication channels
│   │   ├── email_handler.py      # Resend integration (primary)
│   │   └── sms_handler.py        # Africa's Talking (warm leads only)
│   ├── core/                     # Agent logic
│   │   ├── icp_classifier.py     # ICP segment classifier + abstention
│   │   ├── email_drafter.py      # Email composition engine
│   │   ├── conversation.py       # Conversation state manager
│   │   └── orchestrator.py       # End-to-end pipeline
│   ├── enrichment/               # Signal enrichment pipeline
│   │   ├── crunchbase.py         # Crunchbase ODM firmographics
│   │   ├── layoffs.py            # layoffs.fyi parser
│   │   ├── job_posts.py          # Job post scraper (Playwright)
│   │   ├── leadership.py         # Leadership change detection
│   │   ├── ai_maturity.py        # AI maturity scorer (0-3)
│   │   ├── competitor_gap.py     # Competitor gap brief generator
│   │   └── signal_brief.py       # Orchestrator: all signals merged
│   ├── integrations/             # CRM & Calendar
│   │   ├── hubspot.py            # HubSpot MCP integration
│   │   └── calcom.py             # Cal.com booking flow
│   └── observability/            # Tracing & logging
│       ├── langfuse_client.py    # Langfuse tracing
│       └── trace_logger.py       # JSONL trace logger
├── eval/                         # τ²-Bench evaluation harness
│   ├── harness.py                # Benchmark wrapper
│   ├── score_log.json            # Baseline scores with 95% CIs
│   └── trace_log.jsonl           # Full evaluation trajectories
├── data/                         # Data sources
│   ├── crunchbase_odm_sample.json
│   ├── layoffs.csv
│   └── job_posts_snapshot.json
├── tenacious-seeds-placeholder/  # Seed materials (swap with real on Day 0)
│   └── seeds_placeholder/
├── baseline.md                   # τ²-Bench baseline report
├── pyproject.toml                # Project config (uv)
├── .env.example                  # Environment variable template
└── .gitignore
```

## Setup

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager

### Installation

```bash
# Clone the repo
git clone https://github.com/Sanoy24/conversion-engine.git
cd conversion-engine

# Install dependencies with uv
uv sync

# Copy environment template and fill in API keys
cp .env.example .env

# Install Playwright browsers (for job-post scraping)
uv run playwright install chromium
```

### API Keys Required

| Service | Purpose | Get it at |
|---------|---------|-----------|
| OpenRouter | LLM backbone | openrouter.ai |
| Resend | Email (primary channel) | resend.com |
| Africa's Talking | SMS (warm leads) | africastalking.com |
| HubSpot | CRM | developers.hubspot.com |
| Cal.com | Calendar booking | cal.com (self-hosted) |
| Langfuse | Observability | cloud.langfuse.com |

### Running

```bash
# 0. One-time: sync tau2-bench submodule venv
uv sync --directory eval/tau2-bench

# 1. Populate data/ (Crunchbase ODM + layoffs.fyi + job-posts snapshot)
uv run python -m scripts.fetch_data

# 2. τ²-Bench baseline (writes eval/score_log.json + eval/trace_log.jsonl + baseline.md)
uv run python -m eval.harness

# 3. Enrichment → classify → draft pipeline over 25 prospects (writes outputs/e2e_summary.json)
uv run python -m scripts.run_e2e_demo --n 25 --max-parallel 4

# 4. One complete email+SMS+calendar thread for a synthetic prospect
#    (Act II deliverable; kill-switch gated by default)
uv run python -m scripts.run_full_thread_demo

# 5. Rebuild baseline.md + report/interim_report.{md,pdf} from current artifacts
uv run python -m scripts.build_report

# Start the FastAPI server (webhook endpoints for inbound replies)
uv run python -m agent.main

# Process a single prospect ad-hoc
curl -X POST http://localhost:8000/api/prospect/new \
  -H "Content-Type: application/json" \
  -d '{"company_name": "Example Corp", "contact_email": "cto@example.com"}'
```

### Seed Materials (Day-0 deliverable from Tenacious)

The per-challenge seed materials — `style_guide.md`, `bench_summary.md`,
`pricing_sheet.md`, `email_sequences.md`, sales deck, case studies — are
delivered by program staff on Day 0 into `tenacious-seeds-placeholder/seeds_placeholder/`.
Until they arrive, the email drafter falls back to in-code defaults and logs
four "Seed file not found" warnings per run. Drop the real files into that
directory (unchanged filenames, `_PLACEHOLDER` suffix removed) and the warnings
disappear.

## Data Handling Policy

⚠️ **Kill Switch**: The `LIVE_OUTBOUND_ENABLED` environment variable defaults to `false`. When unset, **all outbound messages route to the staff sink**, not to real prospects. Set to `true` only after program staff and Tenacious executive team review and approve.

## Channel Priority

1. **Email** (primary) — founders, CTOs, VPs Engineering live in email
2. **SMS** (secondary) — warm leads who replied by email + prefer fast scheduling
3. **Voice** (bonus) — discovery calls booked by agent, delivered by human

## Key Design Decisions

- **Honesty rule**: Agent refuses claims it cannot ground in the signal brief. Over-claiming damages the brand.
- **Confidence-aware phrasing**: High → ASSERT, Medium → SOFT-ASSERT, Low → ASK
- **Bench-gated commitments**: Never promise capacity the bench summary doesn't show
- **ICP classifier with abstention**: Below-threshold confidence → generic exploratory email
- **Multi-thread leakage prevention**: Per-thread isolation, no cross-thread context sharing
- **Tone-preservation check**: Second LLM call scoring draft against style_guide.md

## License

Challenge-week code. Seed materials under limited license — delete from personal infrastructure after the week.
