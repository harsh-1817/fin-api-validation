# Test strategy

## Scope

Validate the public MFAPI.in mutual-fund scheme endpoint and the shape/quality of its NAV history. The test suite is split into API-level assertions (Postman/Newman) and data-quality checks (Python).

## Rule catalogue

| Rule | Severity | Intent |
|---|---|---|
| HTTP status is 200 | Fail | Endpoint is reachable and returns a successful response |
| JSON content type and parseable body | Fail | Response can be consumed by a client |
| `meta` object exists | Fail | Scheme metadata is present |
| `data` is a non-empty array | Fail | At least one NAV observation is available |
| Each observation has date and NAV | Fail | Required values are populated |
| Date is parseable | Fail | Observation can be sorted and assessed for freshness |
| NAV is numeric and greater than zero | Fail | Reject invalid NAV values |
| Dates are unique | Fail | Detect duplicate observations |
| Latest NAV is within freshness threshold | Fail | Detect potentially stale data; weekends/holidays need context |
| Large consecutive NAV move | Warning | Flag a potentially unusual observation for review |

## Test layers

1. **Unit tests:** use the clearly synthetic fixture; deterministic and network-independent.
2. **API tests:** Postman assertions run against the live public API.
3. **Integration run:** Python fetches live responses and exports reports.

## Exit criteria

The test process should complete, produce the expected report files, and leave a clear audit trail. Failures must be investigated rather than silently discarded. Thresholds should be documented and calibrated before any production use.

## Out of scope

Authentication, paid provider SLAs, trading decisions, investment advice, and production alert delivery.
