import sys
sys.path.insert(0, __import__('pathlib').Path(__file__).resolve().parents[4].as_posix())
from flpcr.chain.sim import SimWorld
from flpcr.chain.driver import Action, AUTH_SIGNATURE, AUTH_MESSAGE
from flpcr.clvm import sha256tree
from flpcr import EngineState, FixtureSource, HolderEvent, Kind, run_epoch
POOL, LP = b"\xb0" * 32, b"\x1b" * 32

def outcome(fn, *a, **k):
    try:
        r = fn(*a, **k)
        return "ACCEPTED %r" % (r,)
    except ValueError as e:
        m = str(e)
        return "REFUSED " + (m if len(m) < 160 else m[:160])

def post_h(w, root, h, sender="engine"):
    totals_list = [(k, v) for k, v in sorted(root.totals.items())]
    args = [root.root_id, root.start, root.end, root.root, totals_list, bytes(32)]
    return w.push([Action("post_root", [h, *args])], [lambda: w.message_from(sender, b"p" + sha256tree(args))])

def veto_h(w, root_id, h):
    return w.push([Action("veto", [h, root_id])], [lambda: w.message_from("guardian", b"v" + sha256tree(root_id))])

def setup(dispute, window=None, length=40):
    w = SimWorld(dispute_blocks=dispute)
    w.advance_to(w.h + 3)
    window = window or 2 * dispute
    camp = w.campaign("creator", 1_000_000, w.h + 2, w.h + 2 + length, Kind.LP_HOLD, POOL, claim_window=window)
    alice, bob = w.keys.ph("alice"), w.keys.ph("bob")
    src = FixtureSource(lp_assets={POOL: LP}, events={LP: [HolderEvent(0, alice, 3), HolderEvent(0, bob, 1)]})
    w.create(camp, "creator")
    return w, camp, src
