import json
from datetime import date
from pathlib import Path

from src.validate_nav_api import parse_nav_date, validate_payload

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample_mfapi_response.json"


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_has_valid_structure_and_positive_navs():
    result, observations = validate_payload(load_fixture(), "999999", today=date(2025, 1, 5))
    by_name = {c["check"]: c["status"] for c in result["checks"]}
    assert by_name["metadata_present"] == "PASS"
    assert by_name["nav_history_nonempty"] == "PASS"
    assert by_name["positive_numeric_nav"] == "PASS"
    assert by_name["duplicate_dates"] == "PASS"
    assert len(observations) == 5


def test_invalid_nav_and_duplicate_date_fail():
    payload = load_fixture()
    payload["data"][1]["nav"] = "0"
    payload["data"][2]["date"] = payload["data"][0]["date"]
    result, _ = validate_payload(payload, "999999", today=date(2025, 1, 5))
    by_name = {c["check"]: c["status"] for c in result["checks"]}
    assert by_name["positive_numeric_nav"] == "FAIL"
    assert by_name["duplicate_dates"] == "FAIL"
    assert result["overall_status"] == "FAIL"


def test_stale_nav_fails():
    result, _ = validate_payload(load_fixture(), "999999", today=date(2025, 2, 1))
    by_name = {c["check"]: c["status"] for c in result["checks"]}
    assert by_name["freshness"] == "FAIL"


def test_large_nav_movement_is_warning():
    payload = load_fixture()
    payload["data"][0]["nav"] = "150.00"
    result, _ = validate_payload(payload, "999999", max_daily_change_pct=20, today=date(2025, 1, 5))
    by_name = {c["check"]: c["status"] for c in result["checks"]}
    assert by_name["large_nav_movements"] == "WARN"


def test_date_parser_rejects_invalid_date():
    assert parse_nav_date("not-a-date") is None
    assert parse_nav_date("05-Jan-2025") == date(2025, 1, 5)
