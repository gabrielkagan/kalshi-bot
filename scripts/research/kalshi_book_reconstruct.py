"""Phase 1b — Kalshi orderbook reconstruction (the C1 fix at the source).

Replays `kalshi_ws/orderbook_delta` bronze (snapshot + deltas) into a coherent
per-market book and derives a NEVER-CROSSED NBBO. Unlike the state.db scalar
yes_ask/yes_bid fields (independently-lagging, cross 22-38% of the time — R1-C1),
a book reconstructed from the actual resting orders cannot cross.

Kalshi binary book convention (verified against real bronze 2026-05-30):
  - `yes_dollars_fp` = resting YES bids; best YES bid  = max(yes prices).
  - `no_dollars_fp`  = resting NO  bids; best YES ask  = 100 - max(no prices)
    (buying YES lifts the best NO bid). yes_ask = 100 - best_no_bid.
  - Prices are sub-cent strings ("0.0880"); keyed in ticks (round(p*1e4)).

Parent plan: kb/decisions/settlement-lag-convergence-edge-spike-plan.md (Phase 1b)
"""

from __future__ import annotations

import json
from typing import Optional, Sequence


def _tick(price_dollars: str) -> int:
    """Price string -> integer ticks of $0.0001 (avoids float-key fragility)."""
    return int(round(float(price_dollars) * 10000))


class KalshiBook:
    """Mutable per-market book: tick -> resting size, for the yes and no sides."""

    __slots__ = ("yes", "no")

    def __init__(self) -> None:
        self.yes: dict[int, float] = {}
        self.no: dict[int, float] = {}

    def apply_snapshot(
        self, yes_levels: Sequence, no_levels: Sequence
    ) -> None:
        """Replace BOTH sides wholesale (a snapshot is the full book state)."""
        self.yes = {_tick(p): float(s) for p, s in yes_levels if float(s) > 0}
        self.no = {_tick(p): float(s) for p, s in no_levels if float(s) > 0}

    def apply_delta(self, side: str, price_dollars: str, delta_fp: str) -> None:
        """Adjust resting size at one level; remove the level if it hits <= 0."""
        book = self.yes if side == "yes" else self.no
        t = _tick(price_dollars)
        new = book.get(t, 0.0) + float(delta_fp)
        if new > 1e-9:
            book[t] = new
        else:
            book.pop(t, None)

    def apply_frame(self, inner: dict) -> None:
        """Dispatch a parsed inner Kalshi frame (snapshot or delta)."""
        t = inner.get("type")
        msg = inner.get("msg", {})
        if t == "orderbook_snapshot":
            self.apply_snapshot(msg.get("yes_dollars_fp", []), msg.get("no_dollars_fp", []))
        elif t == "orderbook_delta":
            self.apply_delta(msg["side"], msg["price_dollars"], msg["delta_fp"])
        # other inner types (e.g. control msgs) are ignored

    def apply_frames(self, inners) -> None:
        """Apply an iterable of inner frames. Same semantics as looping apply_frame.

        Hot path for day-scale bronze replay. Pin equivalence vs apply_frame
        in tests/research/test_kalshi_book_reconstruct.py. Rust/Cython only
        if a profile shows this loop ≥60% of a day's wall time.
        """
        for inner in inners:
            self.apply_frame(inner)

    def best_yes_bid_cents(self) -> Optional[float]:
        live = [t for t, s in self.yes.items() if s > 0]
        return (max(live) / 100.0) if live else None

    def best_yes_ask_cents(self) -> Optional[float]:
        live = [t for t, s in self.no.items() if s > 0]
        return (100.0 - max(live) / 100.0) if live else None

    def best_yes_bid_depth(self) -> Optional[float]:
        """Contracts resting at the best YES bid (what you could SELL into)."""
        live = [t for t, s in self.yes.items() if s > 0]
        return self.yes[max(live)] if live else None

    def best_yes_ask_depth(self) -> Optional[float]:
        """Contracts you could BUY at the best YES ask = size resting at the best
        NO bid (buying YES lifts the NO bid)."""
        live = [t for t, s in self.no.items() if s > 0]
        return self.no[max(live)] if live else None

    def best_no_bid_cents(self) -> Optional[float]:
        """Best resting NO bid (price someone will pay for NO)."""
        live = [t for t, s in self.no.items() if s > 0]
        return (max(live) / 100.0) if live else None

    def best_no_bid_depth(self) -> Optional[float]:
        live = [t for t, s in self.no.items() if s > 0]
        return self.no[max(live)] if live else None


    def is_reliable(self, max_levels_per_side: int = 220) -> bool:
        """A reconstructed book is trustworthy only if it is PHYSICALLY plausible:
        uncrossed (best_yes_bid + best_no_bid <= 100 — a real binary book CAN'T
        sum past 100; this is the true drift signal). The level cap is only a
        backstop against pathological runaway drift — real Kalshi 15M books are
        DEEP (MMs quote ~every cent, ~194 levels measured on clean post-fix data),
        so the cap must be generous; the crossing check does the real work."""
        yb = self.best_yes_bid_cents()
        nb = self.best_no_bid_cents()
        if yb is not None and nb is not None and (yb + nb) > 100.0 + 1e-9:
            return False
        if sum(1 for s in self.yes.values() if s > 0) > max_levels_per_side:
            return False
        if sum(1 for s in self.no.values() if s > 0) > max_levels_per_side:
            return False
        return True


def reliable_nbbo_at(frames: Sequence, cutoff_epoch: float,
                     max_deltas_since_snap: int = 100000,
                     max_levels_per_side: int = 220) -> tuple:
    """Snapshot-ANCHORED NBBO at cutoff (no look-ahead). Resets the book on every
    snapshot (ground truth — they're never crossed), so re-subscribe interleaving
    and pre-snapshot drift are discarded. Returns (None, None) — REFUSING to give
    a price — if: no snapshot anchored the book, too many deltas piled up since the
    last snapshot (drift risk), or the resulting book fails `is_reliable`. This is
    how a corrupted historical stream becomes a smaller-but-trustworthy dataset."""
    b = KalshiBook()
    since_snap = 0
    anchored = False
    for ts, inner in frames:
        if ts > cutoff_epoch:
            break
        if inner.get("type") == "orderbook_snapshot":
            b = KalshiBook()
            b.apply_frame(inner)
            since_snap = 0
            anchored = True
        else:
            b.apply_frame(inner)
            since_snap += 1
    if not anchored or since_snap > max_deltas_since_snap:
        return (None, None)
    if not b.is_reliable(max_levels_per_side):
        return (None, None)
    return b.best_yes_bid_cents(), b.best_yes_ask_cents()


def nbbo_at(frames: Sequence, cutoff_epoch: float) -> tuple:
    """Replay (recv_epoch, inner) frames in order, applying only those with
    recv_epoch <= cutoff_epoch (NO look-ahead past the decision time), and
    return (yes_bid_cents, yes_ask_cents) at the cutoff. `frames` must be sorted
    ascending by recv_epoch."""
    b = KalshiBook()
    for ts, inner in frames:
        if ts > cutoff_epoch:
            break
        b.apply_frame(inner)
    return b.best_yes_bid_cents(), b.best_yes_ask_cents()


def book_at(frames: Sequence, cutoff_epoch: float) -> tuple:
    """Return (KalshiBook, last_applied_epoch) at the cutoff — lets a caller read
    the full state incl. best-level DEPTH and the quote AGE (cutoff - last)."""
    b = KalshiBook()
    last = None
    for ts, inner in frames:
        if ts > cutoff_epoch:
            break
        b.apply_frame(inner)
        last = ts
    return b, last


def simulate_maker_bid_fill(frames: Sequence, post_epoch: float) -> tuple:
    """Post a resting YES bid at the best bid as of `post_epoch`; it FILLS iff
    the best ask later (strictly after post) trades down to <= our bid — i.e. a
    seller crosses to us. Returns (our_bid_cents, filled_bool).

    This is the adverse-selection model: a resting bid fills precisely when the
    market moves DOWN (YES getting cheaper / heading toward NO), so filled
    windows are enriched for eventual losers. `frames` must be (recv_epoch,
    inner) sorted ascending and run to ~close."""
    b = KalshiBook()
    our_bid: Optional[float] = None
    for ts, inner in frames:
        b.apply_frame(inner)
        if ts <= post_epoch:
            bid = b.best_yes_bid_cents()
            if bid is not None:
                our_bid = bid  # latest bid at/before the post
        else:
            if our_bid is None:
                continue
            ask = b.best_yes_ask_cents()
            if ask is not None and ask <= our_bid:
                return our_bid, True
    return our_bid, False


def parse_envelope(line) -> tuple:
    """Bronze JSONL line (str or dict) -> (_wire_recv_ts, inner_frame_dict)."""
    env = json.loads(line) if isinstance(line, str) else line
    inner = json.loads(env["_raw"])
    return env.get("_wire_recv_ts"), inner
