"""Per-tier WS subscription assignment — D1.3 implementation target.

D0.2 operator-selected Option D: capture all 59,904 non-MVE markets at
full tick. Two deployment basis cases per D0.3 §6:

- **Pre-soak (D1.5 initial deploy)**: 7-8 WS connections at the
  10K-per-conn TESTED floor (D0.2 §5.2 strict-tested-floor row).
- **Post-D1.3-F1-clear** (NFL Sunday peak-load soak): consolidate to
  6 connections × 15K subs per conn (recommended layout).

Tier classification: T1-T4 markets get full subscription; T5
entertainment markets are sparse-stream and may produce many small
chunks (D0.3 §15 DEEP_ARCHIVE 40 KB-per-object minimum cost
consideration). Per-tier slot allocation is the subscription_manager's
job; conn-A/B/C/D/E/F naming is part of the bronze path.

D0.2 §F2 followup — recognize Kalshi WS error code 25 and shed
least-active subs before reconnect.
"""
