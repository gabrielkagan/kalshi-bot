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

If a future module here genuinely needs an auth/etc. utility that ``bot/``
also uses, the v1 stance (per D0.3 §5 paragraph 6) is to duplicate the
minimum (~20 LOC for RSA-PSS-SHA256) rather than introduce the first
cross-package coupling. Re-evaluate at D1.5 if drift becomes a problem.

Real submodule bodies land across D1.2-D1.5. Canonical Bit ⇄ submodule
mapping (single source of truth):

  D1.2 — writer.py + uploader.py (the bronze-tape critical path: first
         non-empty chunks landing in S3); main_loop.py + ws_connection.py
         + auth.py are natural prerequisites and may land in the same Bit
         or in tightly-coupled follow-ups (D1.2 itself = "writer +
         uploader code" per the pickup-prompt L46 / D0.3 §0 minimum scope)
  D1.3 — subscription_manager.py (per-tier WS subscription assignment;
         D0.2 F1 NFL Sunday peak-load soak ack)
  D1.4 — rest_snapshot.py (REST-fallback redundancy for catalog refresh)
  D1.5 — systemd unit + collector-start.sh wiring (requires-approval);
         auth.py may also extend at D1.5 if `KALSHI_COLLECTOR_KEY_ID`
         provisioning differs from D0.2's test-key shape (D0.3 §12 item #3
         operator decision pending)

See ``agent_docs/bot_layout.md`` "Data Corpus collector" section + the
``kb/decisions/d1-1-pickup-prompt-may16.md`` "Pickup chain" for the
canonical Bit ordering.
"""
