"""A-4 verification: a key that was NEVER granted claims for a holder via AUTH_DELEGATE, proving the
grant with the delegation slot the same singleton spend creates (spent ephemerally)."""
from chia_wallet_sdk import Coin, CoinSpend
from flpcr.chain.values import to_program
from common import *


def eph_spend(w, holder, delegate):
    inner_ph = w.d.inner_puzzle(w.d.state).tree_hash()
    dpz = w.d.delegation_puzzle(holder, delegate)
    eph = Coin(w.d.coin.coin_id(), dpz.tree_hash(), 0)
    sol = to_program(w.clvm, ((w.d.coin.parent_coin_info, (inner_ph, 1)), inner_ph))
    return CoinSpend(eph, dpz.serialize(), sol.serialize())


def dedupe(spends):
    seen, out = set(), []
    for s in spends:
        k = s.coin.coin_id()
        if k not in seen:
            seen.add(k); out.append(s)
    return out


def _main():
  for asset in (bytes(32), b"\xa5" * 32):
      tag = "XCH" if asset == bytes(32) else "CAT"
      for with_eph in (False, True):
          w, c = world(asset)
          w.create(c, "creator")
          cold, thief = w.sim.bls(1), w.sim.bls(1)
          w.advance_to(c.end + 1)
          r = make_root(w, 1, c.start, c.end, [(c.id, cold.puzzle_hash, 100)])
          w.post(r)
          w.advance_to(w.h + w.cfg.dispute_blocks)
          a, u = w.claim_action(r, c.id, cold.puzzle_hash, auth=AUTH_DELEGATE,
                                auth_data=(thief.puzzle_hash, (AUTH_SIGNATURE, thief.pk.to_bytes())))
          spends, ns, pa = w.d.spend([a])
          spends = dedupe(spends)
          if with_eph:
              spends.append(eph_spend(w, cold.puzzle_hash, thief.puzzle_hash))
          before = w.balance(cold.puzzle_hash)
          attempt(f"{tag} ungranted delegated claim, ephemeral slot spend={with_eph}", w.sim.spend_coins,
                  spends, [thief.sk], expect="accept" if with_eph else "refuse")
          dpz = w.d.delegation_puzzle(cold.puzzle_hash, thief.puzzle_hash)
          print(f"    paid to holder: {w.balance(cold.puzzle_hash) - before}; slots left at ph: "
                f"{len(w.sim.unspent_coins(dpz.tree_hash(), False))}")


if __name__ == '__main__':
    _main()
