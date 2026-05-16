"""Runtime entrypoint — ``python -m collector`` invokes this file.

ENTRYPOINT-SHIM DISCIPLINE (mirrors bot/__main__.py sacred-boundary rule):
this file contains NO business logic. Body lives in
``collector/main_loop.py`` (D1.2 ship). At D1.1 scaffolding, both this
shim and ``main_loop.py`` are stubs — invoking ``python -m collector``
will raise ``NotImplementedError`` until D1.2 wires the writer + WS
ingestion path.

## Import ordering (forward-positioned for future log emitters)

``logging.basicConfig(force=True)`` MUST come BEFORE
``from collector.main_loop import run`` for the same reason that
bot/__main__.py:17-26 hoisted basicConfig above ``from bot.main_loop
import MainLoop`` in Bit 9.3-iii.b: any module-load-time
``logging.getLogger(...).info(...)`` calls triggered by the import
chain would otherwise silent-drop against the unconfigured root logger.
At D1.1 no collector module emits import-time logs (the ordering is
NOT load-bearing today). Once a D1.2+ submodule does (the writer's
rotation handler and the uploader's rclone-result branches are natural
INFO-log sites), the pre-positioned hoist means log lines reach stderr
without an observability regression.

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

from collector.main_loop import run  # noqa: E402 — must come AFTER basicConfig so module-load logs (D1.2+) reach the configured stderr handler


if __name__ == "__main__":
    run()
