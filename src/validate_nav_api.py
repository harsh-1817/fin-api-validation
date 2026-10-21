"""Fetch MFAPI.in NAV history, validate data quality, and export reports."""
from __future__ import annotations

import csv
import html
import json
import math
import os
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

BASE_URL = "https://api.mfapi.in/mf/{scheme_code}"


def parse_nav_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    for fmt in ("%d-%m-%Y", "%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _check(name: str, status: str, message: str, details: Any = None) -> dict[str, Any]:
    item = {"check": name, "status": status, "message": message}
    if details is not None:
        item["details"] = details
    return item


def validate_payload(payload: Any, scheme_code: str, max_stale_days: int = 7,
                     max_daily_change_pct: float = 20.0,
                     today: date | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate an MFAPI-shaped JSON object. Accepts today for deterministic tests."""
    checks: list[dict[str, Any]] = []
    today = today or date.today()
    if not isinstance(payload, dict):
        checks.append(_check("response_is_object", "FAIL", "Response JSON must be an object."))
        return {"scheme_code": scheme_code, "scheme_name": None, "observations": [], "checks": checks}, []

    checks.append(_check("response_is_object", "PASS", "Response JSON is an object."))
    meta = payload.get("meta")
    data = payload.get("data")
    status = payload.get("status")
    if isinstance(meta, dict):
        checks.append(_check("metadata_present", "PASS", "Metadata object is present."))
    else:
        checks.append(_check("metadata_present", "FAIL", "Required metadata object is missing or invalid."))
        meta = {}
    if isinstance(data, list) and len(data) > 0:
        checks.append(_check("nav_history_nonempty", "PASS", f"Found {len(data)} NAV observations."))
    else:
        checks.append(_check("nav_history_nonempty", "FAIL", "NAV history is missing, invalid, or empty."))
        data = []
    if status in (None, "SUCCESS"):
        checks.append(_check("provider_status", "PASS", f"Provider status: {status or 'not supplied'}."))
    else:
        checks.append(_check("provider_status", "FAIL", f"Provider status is {status!r}, not SUCCESS."))

    observations: list[dict[str, Any]] = []
    missing_count = 0
    invalid_nav_count = 0
    invalid_date_count = 0
    seen_dates: set[date] = set()
    duplicate_dates: list[str] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            missing_count += 1
            continue
        raw_date, raw_nav = item.get("date"), item.get("nav")
        parsed_date = parse_nav_date(raw_date)
        try:
            nav = float(raw_nav)
        except (TypeError, ValueError, OverflowError):
            nav = None
        if not raw_date or raw_nav in (None, ""):
            missing_count += 1
        if parsed_date is None:
            invalid_date_count += 1
        if nav is None or not math.isfinite(nav) or nav <= 0:
            invalid_nav_count += 1
            nav = None
        if parsed_date is not None:
            if parsed_date in seen_dates:
                duplicate_dates.append(str(raw_date))
            seen_dates.add(parsed_date)
        observations.append({
            "index": index,
            "date_raw": raw_date,
            "date": parsed_date.isoformat() if parsed_date else None,
            "nav": nav,
        })

    checks.append(_check("required_values", "PASS" if missing_count == 0 else "FAIL",
                         f"Missing date/NAV values or malformed records: {missing_count}.", {"count": missing_count}))
    checks.append(_check("valid_dates", "PASS" if invalid_date_count == 0 else "FAIL",
                         f"Unparseable observation dates: {invalid_date_count}.", {"count": invalid_date_count}))
    checks.append(_check("positive_numeric_nav", "PASS" if invalid_nav_count == 0 else "FAIL",
                         f"NAV values that are non-numeric, zero, or negative: {invalid_nav_count}.", {"count": invalid_nav_count}))
    checks.append(_check("duplicate_dates", "PASS" if not duplicate_dates else "FAIL",
                         f"Duplicate observation dates: {len(duplicate_dates)}.", {"dates": duplicate_dates}))

    valid_dates = [parse_nav_date(o["date_raw"]) for o in observations if o["date"] and o["nav"] and o["nav"] > 0]
    latest_date = max(valid_dates) if valid_dates else None
    stale_days = (today - latest_date).days if latest_date else None
    if latest_date is None:
        checks.append(_check("freshness", "SKIP", "Cannot assess freshness without a valid NAV date."))
    elif stale_days is not None and stale_days > max_stale_days:
        checks.append(_check("freshness", "FAIL", f"Latest valid NAV date is {stale_days} days old (limit: {max_stale_days}).",
                             {"latest_date": latest_date.isoformat(), "age_days": stale_days}))
    else:
        checks.append(_check("freshness", "PASS", f"Latest valid NAV date is {latest_date.isoformat()} ({stale_days} days old).",
                             {"latest_date": latest_date.isoformat(), "age_days": stale_days}))

    valid_rows = sorted((o for o in observations if o["date"] and o["nav"] and o["nav"] > 0), key=lambda x: x["date"])
    unusual: list[dict[str, Any]] = []
    for previous, current in zip(valid_rows, valid_rows[1:]):
        if previous["nav"]:
            change = (current["nav"] / previous["nav"] - 1) * 100
            if abs(change) > max_daily_change_pct:
                unusual.append({"from_date": previous["date"], "to_date": current["date"], "change_pct": round(change, 4)})
    if unusual:
        checks.append(_check("large_nav_movements", "WARN", f"{len(unusual)} consecutive-observation NAV change(s) exceed ±{max_daily_change_pct}% and need review.", unusual))
    else:
        checks.append(_check("large_nav_movements", "PASS", f"No consecutive-observation NAV changes exceeded ±{max_daily_change_pct}%."))

    summary = {
        "scheme_code": str(scheme_code),
        "scheme_name": meta.get("scheme_name"),
        "provider_status": status,
        "observations_received": len(data),
        "latest_nav_date": latest_date.isoformat() if latest_date else None,
        "overall_status": "FAIL" if any(c["status"] == "FAIL" for c in checks) else ("WARN" if any(c["status"] == "WARN" for c in checks) else "PASS"),
        "checks": checks,
    }
    return summary, observations


def fetch_scheme(scheme_code: str, timeout: int) -> tuple[Any | None, str | None]:
    url = BASE_URL.format(scheme_code=scheme_code)
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": "fin-api-validation-portfolio/1.0"})
        response.raise_for_status()
        try:
            return response.json(), None
        except ValueError:
            return None, "Response was not valid JSON."
    except requests.RequestException as exc:
        return None, str(exc)


def write_reports(results: list[dict[str, Any]], all_observations: list[dict[str, Any]], report_dir: Path) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": "MFAPI.in public mutual-fund API",
        "note": "Validation results describe the observed response, not an investment recommendation.",
        "overall_status": "FAIL" if any(r["overall_status"] == "FAIL" for r in results) else ("WARN" if any(r["overall_status"] == "WARN" for r in results) else "PASS"),
        "schemes": results,
    }

    generated_dt = datetime.fromisoformat(
        report["generated_at_utc"].replace("Z", "+00:00")
    )
    ist = timezone(timedelta(hours=5, minutes=30))
    generated_display = generated_dt.astimezone(ist).strftime(
        "%d %b %Y, %I:%M:%S %p IST"
    )
    (report_dir / "validation_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    with (report_dir / "nav_observations.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["scheme_code", "scheme_name", "date", "nav"])
        writer.writeheader()
        writer.writerows(all_observations)

    
    rows = []
    for scheme in results:
        scheme_name = html.escape(str(scheme.get("scheme_name") or "Unknown scheme"))
        scheme_code = html.escape(str(scheme["scheme_code"]))
        status = scheme["overall_status"].lower()
        observations_count = scheme.get("observations_received", 0)
        latest_date = html.escape(str(scheme.get("latest_nav_date") or "N/A"))
        
        # Build a NAV trend chart from the actual observations for this scheme.
        scheme_points = sorted(
            (
                o for o in all_observations
                if str(o.get("scheme_code")) == str(scheme["scheme_code"])
                and o.get("date")
                and o.get("nav") is not None
            ),
            key=lambda o: o["date"]
        )

        chart_html = ""
        if len(scheme_points) >= 2:
            chart_points = scheme_points
            # Keep the report lightweight while preserving the overall trend.
            if len(chart_points) > 250:
                step = (len(chart_points) - 1) / 249
                chart_points = [
                    chart_points[round(i * step)] for i in range(250)
                ]
                if chart_points[-1] != scheme_points[-1]:
                    chart_points.append(scheme_points[-1])

            values = [float(o["nav"]) for o in chart_points]
            low, high = min(values), max(values)
            span = high - low or 1.0

            width, height = 900, 270
            left, right, top, bottom = 70, 25, 25, 55
            plot_width = width - left - right
            plot_height = height - top - bottom

            coords = []
            for i, value in enumerate(values):
                x = left + i * plot_width / (len(values) - 1)
                y = top + (high - value) * plot_height / span
                coords.append(f"{x:.1f},{y:.1f}")

            polyline = " ".join(coords)
            date_start = html.escape(chart_points[0]["date"])
            date_end = html.escape(chart_points[-1]["date"])

            chart_html = f"""
            <section class="chart-panel">
              <div class="chart-heading">
                <div>
                  <h3>NAV history</h3>
                  <p>Historical net asset value across available observations</p>
                </div>
                <span class="chart-range">{date_start} — {date_end}</span>
              </div>
              <div class="chart-wrap">
                <svg viewBox="0 0 {width} {height}"
                     role="img"
                     aria-label="NAV history line chart">
                  <line x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}"
                        stroke="#dbe4f0"/>
                  <line x1="{left}" y1="{height-bottom}"
                        x2="{width-right}" y2="{height-bottom}"
                        stroke="#dbe4f0"/>
                  <line x1="{left}" y1="{top}"
                        x2="{width-right}" y2="{top}"
                        stroke="#edf2f8" stroke-dasharray="4 5"/>
                  <line x1="{left}" y1="{top+plot_height/2:.1f}"
                        x2="{width-right}" y2="{top+plot_height/2:.1f}"
                        stroke="#edf2f8" stroke-dasharray="4 5"/>
                  <polyline points="{polyline}" fill="none"
                    stroke="#2563eb" stroke-width="2.5"
                    stroke-linecap="round" stroke-linejoin="round"/>
                  <text x="{left-10}" y="{top+5}" text-anchor="end"
                    fill="#667085" font-size="12">{high:.4f}</text>
                  <text x="{left-10}" y="{height-bottom}"
                    text-anchor="end" fill="#667085" font-size="12">{low:.4f}</text>
                  <text x="{left}" y="{height-20}" fill="#667085"
                    font-size="12">{date_start}</text>
                  <text x="{width-right}" y="{height-20}" text-anchor="end"
                    fill="#667085" font-size="12">{date_end}</text>
                </svg>
              </div>
              <p class="chart-note">
                Showing {len(chart_points):,} plotted points from
                {len(scheme_points):,} available observations.
                Values are NAV per unit; this chart does not represent investment returns.
              </p>
            </section>
            """
        else:
            chart_html = """
            <section class="chart-panel">
              <h3>NAV history</h3>
              <p>Not enough valid observations to draw a trend chart.</p>
            </section>
            """


        checks = scheme.get("checks", [])
        passed = sum(c["status"] == "PASS" for c in checks)
        failed = sum(c["status"] == "FAIL" for c in checks)
        warnings = sum(c["status"] == "WARN" for c in checks)
        skipped = sum(c["status"] == "SKIP" for c in checks)

        rows.append(f"""
        <section class="scheme">
          <div class="section-heading">
            <div>
              <span class="eyebrow">SCHEME {scheme_code}</span>
              <h2>{scheme_name}</h2>
            </div>
            <span class="status {status}">{html.escape(scheme['overall_status'])}</span>
          </div>

          <div class="kpis">
            <article class="kpi">
              <span>Observations</span>
              <strong>{observations_count:,}</strong>
              <small>NAV records received</small>
            </article>
            <article class="kpi">
              <span>Checks passed</span>
              <strong class="pass">{passed}</strong>
              <small>Successful validations</small>
            </article>
            <article class="kpi">
              <span>Failures</span>
              <strong class="{'fail' if failed else ''}">{failed}</strong>
              <small>Rules requiring correction</small>
            </article>
            <article class="kpi">
              <span>Latest NAV date</span>
              <strong class="date-value">{latest_date}</strong>
              <small>Most recent valid observation</small>
            </article>
          </div>
        
        {chart_html}
          <div class="mini-summary">
            <span><b class="pass">{passed}</b> passed</span>
            <span><b class="fail">{failed}</b> failed</span>
            <span><b class="warn">{warnings}</b> warnings</span>
            <span><b>{skipped}</b> skipped</span>
          </div>

          <h3>Validation results</h3>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Validation check</th><th>Status</th><th>Details</th></tr></thead>
              <tbody>
        """)

        for check in checks:
            check_status = check["status"].lower()
            rows.append(
                f"<tr><td>{html.escape(check['check'])}</td>"
                f"<td><span class='status {check_status}'>{html.escape(check['status'])}</span></td>"
                f"<td>{html.escape(check['message'])}</td></tr>"
            )

        rows.append("</tbody></table></div></section>")

    total_checks = sum(len(s.get("checks", [])) for s in results)
    passed_total = sum(
        c["status"] == "PASS"
        for s in results for c in s.get("checks", [])
    )
    failed_total = sum(
        c["status"] == "FAIL"
        for s in results for c in s.get("checks", [])
    )
    warning_total = sum(
        c["status"] == "WARN"
        for s in results for c in s.get("checks", [])
    )

    page = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Financial API Validation Dashboard</title>
<style>
:root {
  --blue: #175cd3;
  --navy: #123c70;
  --ink: #172b4d;
  --muted: #667085;
  --line: #e5eaf2;
  --bg: #f3f6fb;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 14px/1.6 Inter, "Segoe UI", system-ui, sans-serif;
}
header {
  background: linear-gradient(120deg, #123c70, #175cd3);
  color: white; padding: 34px max(24px, calc((100% - 1180px)/2));
}
header h1 { margin: 0; font-size: clamp(25px, 4vw, 34px); letter-spacing: -.6px; }
header p { margin: 8px 0 0; color: #dbeafe; }
main { max-width: 1180px; margin: 26px auto; padding: 0 22px 36px; }
.topline {
  display: flex; justify-content: space-between; align-items: center;
  gap: 12px; flex-wrap: wrap; margin-bottom: 18px;
}
.panel, .scheme {
  background: white; border: 1px solid #e9eef5; border-radius: 16px;
  box-shadow: 0 5px 22px #17365d0a;
}
.panel { padding: 20px; margin-bottom: 22px; }
.eyebrow {
  color: var(--blue); font-size: 11px; font-weight: 800;
  letter-spacing: 1.1px;
}
.status {
  display: inline-flex; align-items: center; border-radius: 999px;
  padding: 4px 10px; font-size: 11px; font-weight: 800;
  background: #eef2f6; color: #475467; white-space: nowrap;
}
.pass { color: #16804a; }
.fail { color: #c43232; }
.warn { color: #a15c00; }
.skip { color: #667085; }
.status.pass { background: #e7f7ee; color: #16804a; }
.status.fail { background: #feeceb; color: #b42318; }
.status.warn { background: #fff4d6; color: #9a6700; }
.status.skip { background: #eef2f6; color: #667085; }
.overview-grid, .kpis {
  display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 13px;
}
.overview-card, .kpi {
  border: 1px solid var(--line); border-radius: 12px; padding: 16px;
  min-width: 0;
}
.overview-card span, .kpi span { display: block; color: var(--muted); font-size: 12px; }
.overview-card strong { display: block; font-size: 27px; margin-top: 5px; }
.kpi strong { display: block; font-size: 23px; margin-top: 5px; overflow-wrap: anywhere; }
.kpi .date-value { font-size: 19px; }
.kpi small { color: var(--muted); display: block; margin-top: 4px; }
.scheme { padding: 24px; margin-bottom: 22px; }
.section-heading {
  display: flex; justify-content: space-between; align-items: flex-start;
  gap: 14px; margin-bottom: 20px;
}
h2 { margin: 5px 0 0; font-size: 21px; line-height: 1.4; overflow-wrap: anywhere; }
h3 { font-size: 16px; margin: 26px 0 10px; }
.mini-summary { display: flex; gap: 20px; flex-wrap: wrap; padding: 17px 0 0; color: var(--muted); }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; min-width: 570px; }
th, td { padding: 12px 13px; text-align: left; border-bottom: 1px solid var(--line); vertical-align: top; }
th { background: #f3f7fc; color: #475467; font-size: 11px; text-transform: uppercase; letter-spacing: .6px; }
tbody tr:hover { background: #f8faff; }
td:first-child { font-weight: 600; }
.note { color: var(--muted); font-size: 12px; }
footer { color: var(--muted); text-align: center; font-size: 12px; padding: 14px; }
.chart-panel {
  margin-top: 22px;
  padding: 20px;
  border: 1px solid #e5eaf2;
  border-radius: 12px;
  background: #fbfdff;
}
.chart-heading {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 10px;
}
.chart-heading h3 { margin: 0; }
.chart-heading p, .chart-note {
  color: #667085;
  font-size: 12px;
}
.chart-heading p { margin: 4px 0 0; }
.chart-range {
  color: #475467;
  font-size: 12px;
}
.chart-wrap { width: 100%; overflow: hidden; }
.chart-wrap svg { display: block; width: 100%; height: auto; }
.chart-note { margin-bottom: 0; }
@media(max-width:760px) {
  .overview-grid, .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .scheme { padding: 16px; }
  header { padding: 25px 22px; }
}
@media(max-width:420px) {
  .overview-grid, .kpis { grid-template-columns: 1fr 1fr; gap: 8px; }
  .overview-card, .kpi { padding: 11px; }
  .overview-card strong, .kpi strong { font-size: 20px; }
}
</style>
</head>
<body>
<header>
  <h1>Financial API Validation</h1>
  <p>Data quality monitoring for public mutual-fund NAV data · MFAPI.in</p>
</header>
<main>
  <div class="topline">
    <div>
      <span class="eyebrow">VALIDATION RUN</span><br>
      <span>Report generated: """ + html.escape(generated_display) + """</span>
    </div>
    <span class="status """ + report["overall_status"].lower() + """">OVERALL """ + report["overall_status"] + """</span>
  </div>
  <section class="panel">
    <div class="overview-grid">
      <div class="overview-card"><span>Total checks</span><strong>""" + str(total_checks) + """</strong></div>
      <div class="overview-card"><span>Passed</span><strong class="pass">""" + str(passed_total) + """</strong></div>
      <div class="overview-card"><span>Failures</span><strong class="fail">""" + str(failed_total) + """</strong></div>
      <div class="overview-card"><span>Warnings</span><strong class="warn">""" + str(warning_total) + """</strong></div>
    </div>
    <p class="note">A passing result means the configured rules passed for the observed response; it is not a guarantee that every source value is correct. Large NAV movements are flagged for investigation, not automatically classified as bad data.</p>
  </section>
  """ + "".join(rows) + """
  <footer>Generated by the Financial API Testing & Data Validation Framework · Python + Postman/Newman</footer>
</main>
</body>
</html>"""
    (report_dir / "validation_report.html").write_text(page, encoding="utf-8")



def main() -> int:
    scheme_codes = [s.strip() for s in os.getenv("SCHEME_CODES", "119551").split(",") if s.strip()]
    max_stale = int(os.getenv("MAX_STALE_DAYS", "7"))
    max_change = float(os.getenv("MAX_DAILY_CHANGE_PCT", "20"))
    timeout = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))
    report_dir = Path(os.getenv("REPORT_DIR", "reports"))
    results: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    for code in scheme_codes:
        print(f"Validating MFAPI scheme {code}...")
        payload, error = fetch_scheme(code, timeout)
        if error:
            results.append({"scheme_code": code, "scheme_name": None, "observations_received": 0,
                            "latest_nav_date": None, "overall_status": "FAIL",
                            "checks": [_check("api_request", "FAIL", error)]})
            continue
        summary, scheme_observations = validate_payload(payload, code, max_stale, max_change)
        results.append(summary)
        for obs in scheme_observations:
            observations.append({"scheme_code": code, "scheme_name": summary.get("scheme_name"),
                                 "date": obs.get("date"), "nav": obs.get("nav")})
        for check in summary["checks"]:
            print(f"  [{check['status']}] {check['check']}: {check['message']}")
    write_reports(results, observations, report_dir)
    print(f"\nReport files written to: {report_dir.resolve()}")
    return 1 if any(r["overall_status"] == "FAIL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
