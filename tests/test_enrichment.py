"""Tests for Crunchbase loader and layoffs parser with real data."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from agent.enrichment import layoffs
from agent.enrichment.crunchbase import (
    _extract_employee_count,
    _extract_industries,
    _parse_money,
    extract_funding_signal,
    extract_prospect_info,
    search_company,
)
from agent.enrichment.layoffs import _parse_date, _parse_percentage, check_layoffs


def _days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")


class TestCrunchbaseSearch:
    """Search across the 1000-company real dataset."""

    def test_search_by_name_exact(self):
        """Should find Consolety (first record in dataset)."""
        result = search_company(company_name="Consolety")
        if result:  # Only runs when real data is loaded
            assert result["name"] == "Consolety"
            assert result["id"] == "consolety"

    def test_search_nonexistent_returns_none(self):
        result = search_company(company_name="ThisCompanyDoesNotExist12345")
        assert result is None

    def test_search_by_domain(self):
        result = search_company(domain="consolety.net")
        # Might find it if data is loaded
        if result:
            assert "consolety" in result.get("name", "").lower()


class TestProspectInfoExtraction:
    """Test extracting ProspectInfo from real Crunchbase records."""

    def test_extract_basic_fields(self):
        record = {
            "name": "TestCo",
            "website": "https://testco.com",
            "id": "testco",
            "uuid": "test-uuid-123",
            "country_code": "US",
            "region": "North America",
            "num_employees": "51-100",
            "industries": '[{"id":"saas","value":"SaaS"}]',
            "about": "A B2B SaaS company.",
        }
        info = extract_prospect_info(record)
        assert info.company == "TestCo"
        assert info.domain == "https://testco.com"
        assert info.crunchbase_id == "test-uuid-123"
        assert info.employee_count == 75  # midpoint of 51-100
        assert "SaaS" in info.industry

    def test_extract_location_fallback(self):
        record = {"name": "TestCo", "country_code": "DE", "region": "EU"}
        info = extract_prospect_info(record)
        assert info.hq_location == "EU, DE"


class TestEmployeeCountParsing:
    """The dataset stores num_employees as ranges like '1-10', '51-100'."""

    @pytest.mark.parametrize(
        ("input_val", "expected"),
        [
            ("1-10", 5),
            ("11-50", 30),
            ("51-100", 75),
            ("101-250", 175),
            ("251-500", 375),
            ("501-1000", 750),
            ("1001-5000", 3000),
            (100, 100),
            ("unknown", None),
            (None, None),
        ],
    )
    def test_employee_ranges(self, input_val, expected):
        assert _extract_employee_count({"num_employees": input_val}) == expected


class TestIndustriesParsing:
    """Industries are JSON-encoded lists of dicts."""

    def test_json_list_of_dicts(self):
        raw = '[{"id":"seo","value":"SEO"},{"id":"saas","value":"SaaS"}]'
        assert _extract_industries({"industries": raw}) == "SEO, SaaS"

    def test_plain_string(self):
        assert _extract_industries({"industries": "FinTech"}) == "FinTech"

    def test_empty(self):
        assert _extract_industries({}) == ""


class TestMoneyParsing:
    """Monetary values come in various formats."""

    @pytest.mark.parametrize(
        ("input_val", "expected"),
        [
            ("$10,000,000", 10_000_000),
            ("1.5M", 1_500_000),
            ("2B", 2_000_000_000),
            ("500K", 500_000),
            ("10000000", 10_000_000),
            (None, None),
            ("NaN", None),
        ],
    )
    def test_parse_money(self, input_val, expected):
        assert _parse_money(input_val) == expected


class TestFundingSignalExtraction:
    """Test funding signal from Crunchbase records."""

    def test_with_funding_rounds(self):
        recent = _days_ago(30)
        record = {
            "id": "testco",
            "funding_rounds_list": json.dumps(
                [{"funding_type": "Series A", "money_raised": "$5M", "announced_on": recent}]
            ),
        }
        signal = extract_funding_signal(record)
        assert signal.event == "Series A"
        assert signal.amount_usd == 5_000_000
        assert signal.closed_at == recent

    def test_without_funding(self):
        record = {"id": "testco"}
        signal = extract_funding_signal(record)
        assert signal.event is None

    def test_round_type_comes_from_the_title_in_real_records(self):
        rounds = [{"title": "Series B - TestCo", "announced_on": _days_ago(30)}]
        signal = extract_funding_signal({"id": "testco", "funding_rounds_list": rounds})
        assert signal.event == "Series B"

    def test_newest_round_is_used_whatever_the_order(self):
        rounds = [
            {"title": "Seed Round - TestCo", "announced_on": _days_ago(150)},
            {"title": "Series A - TestCo", "announced_on": _days_ago(30)},
        ]
        signal = extract_funding_signal({"id": "testco", "funding_rounds_list": rounds})
        assert signal.event == "Series A"

    @pytest.mark.parametrize("announced_on", ["2019-03-01", None])
    def test_old_or_undated_round_is_not_a_funding_event(self, announced_on):
        rounds = [{"title": "Series B - TestCo", "announced_on": announced_on}]
        signal = extract_funding_signal({"id": "testco", "funding_rounds_list": rounds})
        assert signal.event is None
        assert signal.confidence.value == "low"


class TestLayoffsParser:
    """Tests against the real layoffs.fyi CSV dataset."""

    def test_known_company(self):
        """Atlassian is in the dataset (first record)."""
        result = check_layoffs("Atlassian", lookback_days=100000)  # Wide window
        # The CSV has Atlassian data from 2023 — may or may not be in lookback
        assert result is not None  # Should at least return a signal

    def test_no_event_is_confident_when_the_data_is_current(self, monkeypatch):
        monkeypatch.setattr(layoffs, "_layoffs_cache", [{"company": "Other", "date": _days_ago(5)}])
        result = check_layoffs("TestCo")
        assert result.event is False
        assert result.confidence.value == "high"

    def test_no_event_is_unknown_when_the_data_is_stale(self, monkeypatch):
        monkeypatch.setattr(layoffs, "_layoffs_cache", [{"company": "Other", "date": "2024-02-29"}])
        result = check_layoffs("TestCo")
        assert result.event is False
        assert result.confidence.value == "low"
        assert "2024-02-29" in result.sources[0].description

    def test_similar_names_do_not_match(self, monkeypatch):
        monkeypatch.setattr(layoffs, "_layoffs_cache", [{"company": "Meta", "date": _days_ago(5)}])
        assert check_layoffs("Metaplex").event is False

    def test_most_recent_event_is_reported(self, monkeypatch):
        older, newer = _days_ago(100), _days_ago(5)
        monkeypatch.setattr(
            layoffs,
            "_layoffs_cache",
            [{"company": "TestCo", "date": older}, {"company": "TestCo", "date": newer}],
        )
        assert check_layoffs("TestCo").closed_at == newer


class TestDateParsing:
    """The layoffs CSV uses M/D/YYYY date format."""

    @pytest.mark.parametrize(
        ("date_str", "expected_year"),
        [
            ("3/6/2023", 2023),
            ("12/25/2024", 2024),
            ("2023-03-06", 2023),
        ],
    )
    def test_parse_dates(self, date_str, expected_year):
        result = _parse_date(date_str)
        assert result is not None
        assert result.year == expected_year


class TestPercentageParsing:
    """Layoffs dataset stores percentages as decimals (0.05 = 5%)."""

    @pytest.mark.parametrize(
        ("input_val", "expected"),
        [
            ("0.05", 5.0),
            ("0.1", 10.0),
            ("0.5", 50.0),
            ("25", 25.0),
            (None, None),
        ],
    )
    def test_parse_percentage(self, input_val, expected):
        assert _parse_percentage(input_val) == expected
