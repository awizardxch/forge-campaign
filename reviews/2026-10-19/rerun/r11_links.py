"""FLPCR-11 rerun: address links replayed, backdated, not revocable by the
primary, invisible to watchers, dropped by the CLI (old probe E/e_links.py).

Fix (flpcr/links.py v2, engine.committed_links, manifest): the signed message
is "flpcr-link-v2:<distributor>:<primary>:<member>:<seq>" with seq strictly
increasing per pair; either side can unlink; the log prefix a root used is
committed in the manifest (count + sha256) and a log that rewrites a committed
prefix or appends an entry dated inside a settled window is refused;
FixtureSource.from_json reads "links". Covered by tests/test_links.py
(TestLinks, TestLinkLog, TestPublication).

Same world as the old probe: a Wizard holder (wiz) links an attacker's LP
address (atk); a campaign over [100, 200) is gated on the Wizard."""

from chia_wallet_sdk import PublicKey, SecretKey, Signature, standard_puzzle_hash

from flpcr import Campaign, FixtureSource as FS, HolderEvent as E, Kind, Params, accrue
from flpcr.engine import EngineState, run_epoch
from flpcr.links import Link, sign, verify
from flpcr.model import Holding, Requirement

from common import check, finish

POOL, LP, WIZ = b"\xb0" * 32, b"\x1b" * 32, b"\x33" * 32
DIST, OTHER_DIST = b"\xd1" * 32, b"\xd2" * 32


class A:
    def __init__(self, i):
        self.sk = SecretKey.from_seed(bytes([i]) * 32).derive_synthetic()
        self.ph = standard_puzzle_hash(self.sk.public_key())


def link(p, m, h, seq, dist=DIST):
    mpk, ms = sign(m.sk, dist, p.ph, m.ph, seq)
    ppk, ps = sign(p.sk, dist, p.ph, m.ph, seq)
    return Link(dist, p.ph, m.ph, seq, h, False, mpk, ms, ppk, ps)


def unlink(p, m, h, seq, by):
    pk, s = sign(by.sk, DIST, p.ph, m.ph, seq, unlink=True)
    if by is m:
        return Link(DIST, p.ph, m.ph, seq, h, True, member_pk=pk, member_sig=s)
    return Link(DIST, p.ph, m.ph, seq, h, True, primary_pk=pk, primary_sig=s)


def again(x, h):
    return Link(x.distributor_id, x.primary, x.member, x.seq, h, x.unlink, x.member_pk, x.member_sig,
                x.primary_pk, x.primary_sig)


wiz, atk, other = A(1), A(2), A(3)
c = Campaign(DIST, b"\xcc" * 32, b"\xca" * 32, 1000, 100, 200, Kind.LP_HOLD, POOL,
             Params(gates=(Requirement(Holding.NFT, WIZ),)), fee_bps=0)


def src(links):
    return FS(lp_assets={POOL: LP}, events={LP: [E(0, atk.ph, 10), E(0, other.ph, 10)]},
              nfts={WIZ: [E(0, wiz.ph, 1), E(0, other.ph, 1)]}, address_links=links)


def earned(links):
    return {k.hex()[:4]: int(v) for k, v in accrue(c, src(links), 100, 200).items()}


old = link(wiz, atk, 10, 1)
gone = unlink(wiz, atk, 50, 2, by=atk)
base = earned([old, gone])
check("control (link@10 seq1, member unlinks @50 seq2): atk earns nothing", atk.ph.hex()[:4] not in base, str(base))
check("control: the link counts while live", atk.ph.hex()[:4] in earned([old]), str(earned([old])))
r = earned([old, gone, again(old, 150)])
check("ATTACK replayed old link @150 (seq 1 <= 2) is ignored (was {..., atk: 250})", r == base, str(r))
r = earned([old, unlink(wiz, atk, 50, 2, by=wiz)])
check("primary alone unlinks (was impossible)", atk.ph.hex()[:4] not in r, str(r))
r = earned([old, unlink(wiz, atk, 50, 2, by=other)])
check("ATTACK a third party's unlink is ignored", atk.ph.hex()[:4] in r, str(r))
foreign = link(wiz, atk, 10, 1, dist=OTHER_DIST)
check("ATTACK a link signed for another distributor does not verify here",
      verify(foreign) and atk.ph.hex()[:4] not in earned([foreign]))

# Backdating: verify() still says nothing about height (it is unsigned), so the defense is the log:
state, root = run_epoch(EngineState(DIST, height=100), [c], src([]), 150)
try:
    run_epoch(state, [c], src([link(wiz, atk, 120, 1)]), 200)
    check("ATTACK backdated link (recorded after root 1, dated 120 inside its window) refused", False)
except ValueError as e:
    check("ATTACK backdated link (recorded after root 1, dated 120 inside its window) refused", True, f"({e})")
s1, r1 = run_epoch(EngineState(DIST, height=100), [c], src([link(wiz, atk, 120, 1)]), 150)
check("the root's manifest commits the log prefix it used", r1.link_count == 1)
try:
    run_epoch(s1, [c], src([]), 200)
    check("ATTACK dropping a committed entry refused", False)
except ValueError:
    check("ATTACK dropping a committed entry refused", True)

check("FixtureSource.from_json keeps links (was False)",
      len(FS.from_json({"links": [__import__("flpcr.links", fromlist=["link_to_json"]).link_to_json(old)]}).links()) == 1)
inf = PublicKey.infinity()
ph = standard_puzzle_hash(inf)
check("identity key never signs an unlink",
      not verify(Link(DIST, ph, atk.ph, 2, 0, True, member_pk=inf.to_bytes(), member_sig=Signature.infinity().to_bytes())))
finish("FLPCR-11")
