"""Data Corpus collector — top-level package SIBLING to ``bot/``.

Architecture: kb/decisions/data-corpus-architecture.md (D0.3, ticket
86b9ypn1n). Scaffolding ship: D1.1 (ticket 86b9ypn49, 2026-05-16).

ISOLATION CONTRACT (D0.3 §10):
    collector failure  ⇒  bot keeps trading
    bot failure        ⇒  collector keeps capturing

Structurally enforced — zero ``bot.*`` imports anywhere in this
package. Pinned by the ``[importlinter:contract:collector-no-bot]``
forbidden contract in ``.importlinter`` and the AST defense-in-depth
walk in ``tests/contracts/test_collector_no_bot_imports.py``.

Shared transport: auth + WS + envelope construction live in the
``kalshi_wire/`` top-level sibling package per the 2026-05-16 §5
AMENDMENT to ``kb/decisions/data-corpus-architecture.md`` ("two sides
of the same coin" — bot and collector consume the same transport).
``collector/auth.py`` was DELETED in D1.1.5 (`f560d30`, ticket
``86b9zdhz2``) — auth flows through ``kalshi_wire.auth`` directly.
The pre-AMENDMENT stance ("duplicate the minimum RSA-PSS in collector")
is SUPERSEDED.

Canonical Bit ⇄ submodule mapping (single source of truth):

  D1.2 — writer.py + uploader.py + main_loop.py body + ws_connection.py
         BronzeArchiver.run() body (SHIPPED 2026-05-16, ticket
         ``86b9ypn66``). The "bronze data plumbing" is now complete —
         envelopes from kalshi_wire flow through BronzeArchiver → writer
         → rotation → uploader → S3.
  D1.3 — subscription_manager.py (per-tier WS subscription assignment;
         D0.2 F1 NFL Sunday peak-load soak ack) + BronzeArchiver.on_session_start
         dispatch + sid→channel binding + main_loop multi-conn fan-out
         (SHIPPED 2026-05-16, ticket ``86b9ypn72``). FIRST Bit where bronze
         files actually populate end-to-end — R1-C3 acceptance criterion
         deferred from D1.2 closed here.
  D1.4 — rest_snapshot.py body + RestSnapshotRefresher + main_loop
         REST-driven catalog refresh wiring (SHIPPED 2026-05-16, ticket
         ``86b9ypn8r``). Kalshi REST ``/markets?status=open`` paginated
         fetch via ``kalshi_wire.auth.make_rest_headers`` replaces the
         file-based seam from D1.3 as the production source of tickers;
         hourly refresh + on-change reconnect keeps the subscription
         set in lockstep with Kalshi's open-market universe.
  D1.5 — systemd unit + collector-start.sh wiring (SHIPPED 2026-05-16,
         ticket ``86b9ypna4``, requires-approval discipline tier).
         Installs ``ops/kalshi-collector.service`` + extends
         ``ops/install.sh`` to a multi-unit installer + refreshes the
         ``collector-start.sh`` body to source a DEDICATED home-rooted
         ``.env.collector`` (separate from the bot's repo-rooted
         ``.env``). 3 D0.3 §12 operator decisions resolved at kickoff:
         (1) lifecycle Standard → DEEP_ARCHIVE @ 30d (skip IA — matches
         existing journals/ precedent, saves ~$140/yr); (2) ``Nice=10``
         (I/O-bound, not real-time); (3) ``KALSHI_COLLECTOR_KEY_ID``
         provisioned via operator runbook into ``.env.collector``.
         Bronze day-zero is the first-chunk-in-S3 timestamp after
         ``systemctl start kalshi-collector``.

See ``agent_docs/bot_layout.md`` "Data Corpus collector" section + the
``kb/decisions/d1-1-pickup-prompt-may16.md`` "Pickup chain" for the
canonical Bit ordering.
"""
