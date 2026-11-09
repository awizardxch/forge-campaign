"""FLPCR-04 rerun: fill the campaign table with 1-mojo campaigns nobody will
reclaim (old probes D/d3_fill.py, C/probe_c.py P1, C/probe_c2.py Q1).

Fixed differently from the review's sweep proposal (decision recorded in
CLAUDE.md): the admin (dev multisig) may reclaim any campaign from
end + claim_window, paid to the creator only; create.rue bounds `start` to
h + max_campaign_length; the router's RouterRules set a minimum amount and
per-day rate per asset and a live-campaign cap per creator. Covered by
tests/test_chain.py::TestOnlyTheCreatorReclaims::test_admin_reclaims_paid_to_creator,
TestAdminWaitsForTheWindow, TestStartAndWindow::test_start_horizon,
TestAssertionProbes::test_create_campaign_cap, tests/test_router.py::
test_rules_floor_and_creator_cap.

The table cap itself is unchanged, so the original attack still fills it; what
changed is that it no longer locks `create` for good. Run here with
max_campaigns = 2 (the old probe used 32; the leaf check is the same)."""

from flpcr import Campaign, Kind
from flpcr.router import ForgeRegistry, NotRoutable, RouterRules, check_campaign

from common import ASSETS, LEAD, POOL, attempt, check, final_root, finish, settle, src, world

PARAMS = [10000, 0, 0, None, None, 1]


def run(tag, asset):
    print(f"\n{tag}")
    w, c = world(asset, max_campaigns=2, max_campaign_length=200)
    window = c.claim_window
    s = w.h + LEAD + 2
    for nonce in (0, 1):
        attempt(f"griefer 1-mojo campaign #{nonce + 1}", w.create_raw, "griefer", 1, s, s + 16, 1, POOL, PARAMS,
                nonce, window)
    check("table full: 2 campaigns, reserve 2", len(w.state().campaigns) == 2 and w.state().reserve == 2)
    # create.rue: `count(state.campaigns) < CONFIG.max_campaigns` is the only check this honest
    # create can fail (every other field is the baseline TestAssertionProbes accepts).
    attempt("honest create while the table is full", w.create, c, "creator", expect="refuse", reason="clvm raise")
    # FLPCR-04's far-future start: start beyond h + max_campaign_length (200 here).
    h = w.h
    attempt("ATTACK griefer start beyond the horizon (h + max_campaign_length + 1)", w.create_raw, "griefer", 1,
            h + 201, h + 241, 1, POOL, PARAMS, 9, window, h=h, expect="refuse", reason="clvm raise")
    griefer_ids = [x.id for x in w.state().campaigns]
    end = max(x.end for x in w.state().campaigns)
    w.advance_to(end + window - 1)
    attempt("admin reclaim one block before end + claim_window", w.reclaim, griefer_ids[0], admin=True,
            expect="refuse", reason="clvm raise")
    w.advance_to(end + window)
    attempt("stranger reclaims a griefer row (creator message sent by a stranger)", w.reclaim, griefer_ids[0],
            sender="stranger", expect="refuse", reason="MessageNotSentOrReceived")
    attempt("stranger poses as the admin", w.reclaim, griefer_ids[0], admin=True, admin_sender="stranger",
            expect="refuse", reason="MessageNotSentOrReceived")
    paid = attempt("FIX: admin reclaims a griefer row, paid to the griefer (creator)", w.reclaim, griefer_ids[0],
                   admin=True)
    check("admin reclaim paid the creator exactly its leftover", paid == 1, f"(paid {paid})")
    w.reclaim(griefer_ids[1], admin=True)
    c2 = w.campaign("creator", 1_000_000, w.h + LEAD + 2, w.h + LEAD + 42, Kind.LP_HOLD, POOL, claim_window=window)
    attempt("honest create after the admin drained the table", w.create, c2, "creator")

    # Honest wear: a fully claimed campaign keeps its row; the admin removes it, paying 0.
    r = final_root(w, c2, src((w.keys.ph("alice"), 1)))
    w.post(r)
    settle(w)
    w.claim(r, c2.id, "alice")
    check("fully claimed: claimed == budget", w.state().campaign(c2.id).claimed == c2.budget)
    w.advance_to(c2.end + window)
    attempt("FIX: admin removes the fully claimed row (pays 0)", w.reclaim, c2.id, admin=True)
    check("row gone", w.state().campaign(c2.id) is None)


for tag, asset in ASSETS:
    run(tag, asset)

print("\nrouter")
XCH = bytes(32)
reg = ForgeRegistry.from_pools({POOL: [XCH]})
tiny = Campaign(b"\xd1" * 32, b"\xcc" * 32, XCH, 1, 100, 116, Kind.LP_HOLD, POOL)
rules = RouterRules(min_amount={XCH: 1000})
try:
    check_campaign(tiny, reg, rules)
    check("router with min_amount 1000 refuses a 1-mojo campaign", False)
except NotRoutable as e:
    check("router with min_amount 1000 refuses a 1-mojo campaign", True, f"({e})")
try:
    check_campaign(tiny, reg, RouterRules())
    print("  [NOTE] RouterRules() defaults set no floor: a 1-mojo campaign is routable until the owner sets values")
except NotRoutable as e:
    print(f"  [NOTE] default rules refuse: {e}")
try:
    check_campaign(tiny, reg, RouterRules(), live_for_creator=4)
    check("router refuses a 5th live campaign for one creator (default cap 4)", False)
except NotRoutable:
    check("router refuses a 5th live campaign for one creator (default cap 4)", True)
finish("FLPCR-04")
