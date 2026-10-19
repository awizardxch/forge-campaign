"""Reviewer C probes: value conservation, reserve, CAT ring, arithmetic.
Run: PYTHONPATH=<repo> python probe_c.py [xch|cat]"""
import sys, traceback
from flpcr.chain.sim import SimWorld
from flpcr.chain.driver import Action, Distributor, AUTH_MESSAGE
from flpcr import EngineState, FixtureSource, HolderEvent, Kind, run_epoch
from chia_wallet_sdk import CatSpend, Spend, Cat, CatInfo, Coin, LineageProof

POOL, LP = b"\xb0" * 32, b"\x1b" * 32
WINDOW = 16
PARAMS = [10000, 0, 0, None, None, 1]
ASSET = bytes(32) if (len(sys.argv) < 2 or sys.argv[1] == "xch") else b"\xa5" * 32


def outcome(label, fn, *a, **k):
    try:
        r = fn(*a, **k)
        print(f"[ACCEPTED] {label} -> {r!r}"[:300])
        return True
    except ValueError as e:
        m = str(e)
        tag = "clvm raise" if "clvm raise" in m else (m[m.find("Simulator error"):][:90] if "Simulator error" in m else "PYERR " + m[:120])
        print(f"[REFUSED ] {label} :: {tag}")
        return False
    except Exception as e:
        print(f"[PYERR   ] {label} :: {type(e).__name__}: {str(e)[:150]}")
        return False


def invariant(w, label=""):
    st = w.d.read_state()
    owed = sum(x["budget"] - x["claimed"] for x in st["campaigns"].values())
    chain = w.reserve_on_chain()
    ok = st["reserve"] == owed == chain
    print(f"    invariant{label}: state={st['reserve']} owed={owed} chain={chain} {'OK' if ok else 'BROKEN'}")
    return ok


def world(**kw):
    w = SimWorld(reward_asset=ASSET, **kw)
    w.advance_to(w.h + 3)
    return w


def raw(w, **kw):
    f = dict(creator="creator", amount=1000, start=w.h + 2, end=w.h + 40, kind=1, target=POOL,
             params=list(PARAMS), nonce=0, claim_window=WINDOW)
    f.update(kw)
    return w.create_raw(**f)


def setup_campaign(w, amount=1_000_000, holders=(("alice", 3), ("bob", 1)), nonce=0, creator="creator"):
    c = w.campaign(creator, amount, w.h + 2, w.h + 42, Kind.LP_HOLD, POOL, claim_window=WINDOW, nonce=nonce)
    w.create(c, creator)
    return c


def src_for(w, holders):
    return FixtureSource(lp_assets={POOL: LP},
                         events={LP: [HolderEvent(0, w.keys.ph(h), n) for h, n in holders]})


def make_root(w, camps, src, end):
    w.advance_to(end + 1)
    _, r = run_epoch(EngineState(w.launcher_id, height=min(c.start for c in camps)), camps, src, end)
    return r


