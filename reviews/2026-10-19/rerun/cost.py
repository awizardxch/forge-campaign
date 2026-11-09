"""CLVM cost on the current ten leaves, XCH and CAT (old probe D/d6_cost.py
measured XCH only, on the eight-leaf build).

For each action: the singleton spend's CLVM execution cost (the leaf run
inside the singleton, action layer and merkle proof), and the whole bundle
the simulator accepted, costed by chia_wallet_sdk.spend_bundle_cost: every
spend in it (singleton, reserve, slots, the bond launcher and bond, and the
stand-in coins that send the messages, pay the fee or fund a deposit),
including the per-byte, CREATE_COIN and AGG_SIG charges. Block limit 11e9.

Writes a markdown table to stdout (pasted into README.md)."""

import chia_wallet_sdk as sdk

from flpcr import EngineState, Kind, run_epoch
from flpcr.chain.driver import MAX_COST

from common import ASSETS, LEAD, POOL, ByDelegate, BySignature, HEIGHT_WINDOW, settle, src, world

BLOCK = 11_000_000_000


class Recorder:
    """Forwards to the simulator and remembers the last bundle it accepted."""

    def __init__(self, sim):
        self._sim, self.last = sim, None

    def spend_coins(self, spends, keys):
        out = self._sim.spend_coins(spends, keys)
        self.last = list(spends)
        return out

    def __getattr__(self, name):
        return getattr(self._sim, name)


ROWS = []


def measure(label, w, fn):
    w.sim.last = None
    fn()
    spends = w.sim.last
    single = next(s for s in spends if s.coin.coin_id() == spends[0].coin.coin_id())
    run = w.clvm.deserialize(single.puzzle_reveal).run(w.clvm.deserialize(single.solution), MAX_COST, True).cost
    total = sdk.spend_bundle_cost(spends)
    ROWS.append((label, run, total))


def run(tag, asset):
    w, c = world(asset, max_campaigns=32)
    S = w.h + LEAD + 40  # room for 32 creates, one block each, before start - 32
    c = w.campaign("creator", 1_000_000, S, S + 40, Kind.LP_HOLD, POOL, claim_window=c.claim_window)
    w.sim = Recorder(w.sim)
    wal = w.bls()
    hot = w.bls()
    holders = [w.keys.ph(f"h{i}") for i in range(8)]
    s = src(*[(ph, i + 1) for i, ph in enumerate(holders)], (wal.puzzle_hash, 3))
    measure(f"{tag} create (empty table)", w, lambda: w.create(c, "creator"))
    camps = [c]
    for i in range(1, 32):
        ci = w.campaign(f"cr{i}", 1_000_000 + i, c.start, c.end, Kind.LP_HOLD, POOL, claim_window=c.claim_window, nonce=i)
        if i == 31:
            measure(f"{tag} create (32nd, 31 in the table)", w, lambda: w.create(ci, f"cr{i}"))
        else:
            w.create(ci, f"cr{i}")
        camps.append(ci)
    w.advance_to(c.start + 21)
    st1, r1 = run_epoch(EngineState(w.launcher_id, height=c.start), [c], s, c.start + 20)
    # Two runs over the same chain: 1 campaign in the root, and all 32.
    stA, rA = run_epoch(EngineState(w.launcher_id, height=c.start), camps, s, c.start + 20)
    w2, c2 = world(asset)
    w2.sim = Recorder(w2.sim)
    w2.create(c2, "creator")
    w2.advance_to(c2.start + 21)
    st2, r2a = run_epoch(EngineState(w2.launcher_id, height=c2.start), [c2], s, c2.start + 20)
    measure(f"{tag} post_root, 1 campaign", w2, lambda: w2.post(r2a))
    measure(f"{tag} post_root, 32 campaigns (max_campaigns)", w, lambda: w.post(rA))
    settle(w)
    measure(f"{tag} claim, first (insert, coin message), 32 in state", w, lambda: w.claim(rA, c.id, "h7"))
    w.advance_to(c.end + 1)
    stB, rB = run_epoch(stA, camps, s, c.end)
    measure(f"{tag} post_root, 32 campaigns, with previous totals", w, lambda: w.post(rB))
    settle(w)
    measure(f"{tag} claim, repeat (own slot), 32 in state", w, lambda: w.claim(rB, c.id, "h7"))
    measure(f"{tag} claim, first, key signature (AGG_SIG_UNSAFE)", w,
            lambda: w.push([w.claim_call(rB, c.id, wal.puzzle_hash, auth=BySignature(wal.pk.to_bytes()))]))
    measure(f"{tag} delegate (grant, insert)", w, lambda: w.set_delegate("h0", hot.puzzle_hash))
    measure(f"{tag} claim, delegated (delegate signs)", w,
            lambda: w.claim_delegated(rB, c.id, holders[0], hot, signed=True))
    w.advance_to(c.end + c.claim_window)
    measure(f"{tag} reclaim (creator, coin message)", w, lambda: w.reclaim(c.id))
    measure(f"{tag} reclaim (admin)", w, lambda: w.reclaim(camps[1].id, admin=True))

    w3, c3 = world(asset)
    w3.sim = Recorder(w3.sim)
    w3.create(c3, "creator")
    w3.advance_to(c3.start + 21)
    _, r3 = run_epoch(EngineState(w3.launcher_id, height=c3.start), [c3], s, c3.start + 20)
    w3.post(r3)
    measure(f"{tag} dispute (bond launcher + bond)", w3, lambda: w3.dispute(r3.root_id))
    measure(f"{tag} resolve (upheld, bond refund)", w3, lambda: w3.resolve(1))


for tag, asset in ASSETS:
    run(tag, asset)
print("| Action | Singleton CLVM cost | Whole bundle cost | % of block |")
print("|---|---:|---:|---:|")
for label, single, total in ROWS:
    print(f"| {label} | {single / 1e6:.2f}M | {total / 1e6:.1f}M | {100 * total / BLOCK:.2f}% |")
