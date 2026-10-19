"""A-4 on reclaim: a stranger triggers the creator's reclaim (paid to the creator) with no grant,
ending holders' late claims. Control: same spend without the ephemeral slot spend."""
from a4_verify import eph_spend, dedupe
from common import *

for asset in (bytes(32), b"\xa5" * 32):
    tag = "XCH" if asset == bytes(32) else "CAT"
    for with_eph in (False, True):
        w, c = world(asset)
        w.create(c, "creator")
        stranger = w.sim.bls(1)
        w.advance_to(c.end + WINDOW + 1)
        creator = w.keys.ph("creator")
        act = Action("reclaim", [w.h, c.id, AUTH_DELEGATE, (stranger.puzzle_hash, (AUTH_SIGNATURE, stranger.pk.to_bytes()))])
        spends = dedupe(w.d.spend([act])[0])
        if with_eph:
            spends.append(eph_spend(w, creator, stranger.puzzle_hash))
        before = w.balance(creator)
        attempt(f"{tag} stranger reclaim, ephemeral={with_eph}", w.sim.spend_coins, spends, [stranger.sk],
                expect="accept" if with_eph else "refuse")
        print("    paid to creator:", w.balance(creator) - before)
