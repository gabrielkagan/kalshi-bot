"""RSA-PSS-SHA256 auth for the collector's Kalshi API key — D1.2/D1.5.

Separate API key from the bot per D0.3 §6 + §10 mechanism #4:
``KALSHI_COLLECTOR_KEY_ID`` (NOT ``KALSHI_API_KEY_ID``). Provisioning
decision pending — see D0.3 §12 item #3: rotate D0.2's test key
(``KALSHI_CORPUS_TEST_KEY_ID`` at ``~/.kalshi/kalshi-corpus-test.pem``)
into production, or generate a new dedicated key.

Implementation pattern: ~20 LOC mirroring ``bot/kalshi_client.py:60``.
D0.3 §5 paragraph 6 declines extracting a shared ``kalshi_auth/`` package
in v1 to avoid the first cross-package coupling that future refactors
would have to preserve.
"""
