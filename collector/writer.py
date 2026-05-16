"""Bronze JSONL writer + rotation — D1.2 implementation target.

D0.3 §2 envelope (6 fields, irreversible): ``_wire_recv_ts`` + ``_source``
+ ``_conn`` + ``_channel`` + ``_collector_seq`` + ``_raw``. Captured at
frame ingress BEFORE any deserialization — ``_raw`` is the full wire
payload as a string, NOT a parsed dict (parsing would let bronze drift
from "what the wire actually said").

D0.3 §4 rotation: 5-minute timer OR 100MB whichever first. Per-conn,
per-channel rotation cadence; rotation handler hands off to
``collector/uploader.py``.

D0.3 §3 partition path:
``bronze/{source}/{channel}/year=YYYY/month=MM/day=DD/hour=HH/conn=<X>/<chunk>.jsonl.zst``
"""
