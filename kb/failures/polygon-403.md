---
status: resolved
updated: 2026-03-17
tags: [failure, polygon, spx, data-feed]
severity: major
---
# Polygon.io 403 Errors

## Summary
Polygon.io REST API returns 403 Forbidden errors intermittently, breaking the SPX hourly engine's price feed and HAR-RV volatility model. Caused zero SPX evaluations for days at a time. SPX hourly was briefly live (Mar 17, 2026) then reverted to observation due to this issue.

## Symptom
- `spx_engine.py` logs 403 errors on Polygon REST calls
- Zero rows in `evaluated_opportunities` for `product_type='spx_hourly'`
- SPX hourly dashboard panel shows stale or empty data
- HAR-RV model (hourly alternative shadow) also broken — depends on Polygon for historical SPX data

## Root Cause
Polygon.io API intermittently returns 403 for valid API keys. Suspected causes:
- Rate limiting not documented at the plan tier
- IP-based throttling on the VPS
- Intermittent auth issues on Polygon's side

The 403 is not a permissions error (the API key has correct scopes) — it's transient.

## Impact
- SPX hourly was live for a brief window on March 17, 2026
- Reverted to `SPX_HOURLY_OBSERVATION_ONLY = True` because the vol engine can't produce probabilities without price data
- HAR-RV SPX shadow strategy also dead during outages

## Mitigation
- **Finnhub fallback:** SPX engine falls back to Finnhub for SPX spot price
- **Finnhub limitation:** Less granular data, possible delays, also not 100% reliable
- SPX hourly remains observation-only until a reliable price feed is established

## Lessons
1. External data dependencies are single points of failure — always have a fallback
2. "Observation mode" is the correct response when a data source is unreliable, not trying to trade through it
3. Monitor evaluation counts per product_type as a health metric (zero evals = broken feed)

## Status
Ongoing. SPX hourly observation-only. No fix for Polygon 403 — it's their infrastructure.

## Related
