"""Runtime entrypoint — ``python -m collector`` invokes this file.

ENTRYPOINT-SHIM DISCIPLINE (mirrors bot/__main__.py sacred-boundary rule):
this file contains NO business logic. Body lives in
``collector/main_loop.py``. **D1.2 SHIPPED 2026-05-16 (ticket
`86b9ypn66`)** + **D1.3 SHIPPED 2026-05-16 (ticket `86b9ypn72`)**:
``run()`` orchestrates SubscriptionManager + per-conn writers +
archivers + uploader in multi-conn per-channel shape; invoking
``python -m collector`` boots the bronze pipeline. First-bronze-flow
landed at D1.3 — ``on_session_start`` dispatches the pre-built
subscribe frames assembled by ``collector/subscription_manager.py``.

## Import ordering (load-bearing post-D1.2)

``logging.basicConfig(force=True)`` MUST come BEFORE
``from collector.main_loop import run`` for the same reason that
bot/__main__.py:17-26 hoisted basicConfig above ``from bot.main_loop
import MainLoop`` in Bit 9.3-iii.b: any module-load-time
``logging.getLogger(...).info(...)`` calls triggered by the import
chain would otherwise silent-drop against the unconfigured root logger.
Post-D1.2, ``collector.main_loop`` + ``collector.uploader`` + ``collector
.ws_connection`` all use module-level ``logging.getLogger(__name__)``;
the pre-positioned basicConfig keeps INFO/ERROR lines reaching stderr.

## bot._thread_env invariant does NOT apply here

bot/__main__.py's ``import bot._thread_env`` (FIRST non-stdlib import,
sets OMP_NUM_THREADS=1) is required because the bot's import chain
transitively loads numpy/scipy/torch via ``bot.models`` /
``scripts.cal_mlp.integration``. The collector is I/O-bound by design
(per D0.3 §5 "Collector process shape — separate Python process" + §6
``Nice=10`` polite-background isolation) — websocket-client, requests,
cryptography, and zstandard are all stdlib-extension C deps that do
NOT cache OpenBLAS thread state at load time. If a future D1.x
submodule does pull in numpy/scipy, mirror bot's ``_thread_env``
pattern at that time rather than carrying the import here preemptively.
"""
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stderr)],
    force=True,
)

from collector.main_loop import run  # noqa: E402 — must come AFTER basicConfig so module-load logs (post-D1.2) reach the configured stderr handler


if __name__ == "__main__":
    run()
