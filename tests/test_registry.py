"""The distributor registry: one current distributor per (reward asset, router), and no
entry trusted until the chain replays it with this build's leaves."""

import json
import tempfile
import unittest

try:
    from flpcr.chain.sim import SimWorld
    from flpcr.registry import (CURRENT, RETIRED, Entry, Registry, RegistryError, entry_from_chain, main,
                                pins_fingerprint, verify, verify_entry)
    from flpcr.service.chain import SimChain

    HAVE_CHAIN = True
except (ImportError, FileNotFoundError):  # pragma: no cover
    HAVE_CHAIN = False

XCH = "00" * 32


def entry(n: int, asset: str = XCH, router: str = "aa" * 32, **kw) -> "Entry":
    return Entry(f"{n:02x}" * 32, asset, router, "bb" * 32, "cc" * 32, "dd" * 32, "ee" * 32, **kw)


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestRegistryFile(unittest.TestCase):
    def test_a_newer_distributor_retires_the_older_one_for_its_key(self):
        reg = Registry("testnet11")
        self.assertIsNone(reg.add(entry(1)))
        self.assertIsNone(reg.add(entry(2, asset="11" * 32)))  # another reward asset: its own key
        self.assertIsNone(reg.add(entry(3, router="ab" * 32)))  # another operator: its own key
        old = reg.add(entry(4))
        self.assertEqual((old.launcher_id, old.status), ("01" * 32, RETIRED))
        self.assertEqual([e.launcher_id[:2] for e in reg.current(XCH)], ["03", "04"])
        self.assertEqual([e.launcher_id[:2] for e in reg.current(XCH, "aa" * 32)], ["04"])
        self.assertEqual(len(reg.current()), 3)

    def test_what_it_refuses(self):
        reg = Registry("testnet11", [entry(1)])
        with self.assertRaisesRegex(RegistryError, "already listed"):
            reg.add(entry(1))
        with self.assertRaisesRegex(RegistryError, "lists a current"):
            reg.add(entry(2, status=RETIRED))
        with self.assertRaisesRegex(RegistryError, "two current"):
            Registry("testnet11", [entry(1), entry(2)])
        with self.assertRaisesRegex(RegistryError, "listed twice"):
            Registry("testnet11", [entry(1), entry(1, status=RETIRED)])
        with self.assertRaisesRegex(RegistryError, "network"):
            Registry("testnet10")
        with self.assertRaisesRegex(RegistryError, "32 bytes"):
            Entry("zz", XCH, "aa" * 32, "bb" * 32, "cc" * 32, "dd" * 32, "ee" * 32)
        with self.assertRaisesRegex(RegistryError, "http"):
            entry(5, api="ftp://x")
        with self.assertRaisesRegex(RegistryError, "unknown launcher"):
            reg.retire("09" * 32)

    def test_it_round_trips_through_its_file(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(Registry.load("mainnet", d).entries, [])  # no file yet: empty
            reg = Registry("testnet11", [entry(1, status=RETIRED, note="old pins"), entry(2, api="https://x.test/flpcr")])
            path = reg.save(d)
            self.assertEqual(Registry.load("testnet11", d).to_json(), reg.to_json())
            path.rename(path.with_name("mainnet.json"))
            with self.assertRaisesRegex(RegistryError, "is for testnet11"):
                Registry.load("mainnet", d)
            raw = reg.to_json() | {"version": 2}
            with self.assertRaisesRegex(RegistryError, "version"):
                Registry.from_json(raw)

    def test_the_committed_registries_load(self):
        for network in ("testnet11", "mainnet"):
            reg = Registry.load(network)
            self.assertEqual(reg.network, network)
            for e in reg.current():  # a current entry is one this build can replay
                self.assertEqual(e.pins, pins_fingerprint())


@unittest.skipUnless(HAVE_CHAIN, "chia-wallet-sdk or compiled puzzles missing")
class TestRegistryAgainstTheChain(unittest.TestCase):
    def setUp(self):
        self.w = SimWorld()
        self.w.advance_to(self.w.h + 3)
        self.reader = SimChain(self.w.sim).reader

    def test_an_entry_is_read_off_the_chain_and_verifies(self):
        w = self.w
        e = entry_from_chain(self.reader, w.launcher_id, api="https://x.test/flpcr")
        cfg = w.d.cfg
        self.assertEqual((e.reward_asset, e.router_ph, e.guardian_ph, e.admin_ph, e.dev_fee_ph),
                         (cfg.reward_asset.hex(), cfg.router_ph.hex(), cfg.guardian_ph.hex(), cfg.admin_ph.hex(),
                          cfg.dev_fee_ph.hex()))
        self.assertEqual((e.status, e.pins), (CURRENT, pins_fingerprint()))
        reg = Registry("testnet11", [e])
        self.assertEqual(verify(self.reader, reg), {w.launcher_id.hex(): None})

    def test_an_entry_the_chain_contradicts_fails_for_its_own_reason(self):
        w = self.w
        good = entry_from_chain(self.reader, w.launcher_id)
        for name in ("reward_asset", "router_ph", "guardian_ph", "admin_ph", "dev_fee_ph"):
            bad = Entry(**{**good.__dict__, name: "f0" * 32})
            self.assertRegex(verify_entry(self.reader, bad), f"^{name} on chain is ")
        self.assertRegex(verify_entry(self.reader, Entry(**{**good.__dict__, "pins": "ab" * 32})), "^listed with pins ")
        self.assertRegex(verify_entry(self.reader, Entry(**{**good.__dict__, "launcher_id": "12" * 32})),
                         "^(could not be read from chain|does not replay)")
        # A retired entry is not checked: this build cannot replay older leaves.
        self.assertEqual(verify(self.reader, Registry("testnet11", [Entry(**{**good.__dict__, "status": RETIRED,
                                                                            "pins": "ab" * 32})])), {})

    def test_the_service_serves_it(self):
        from flpcr.service.core import CampaignService

        w = self.w
        reg = Registry("testnet11", [entry_from_chain(self.reader, w.launcher_id), entry(1, status=RETIRED)])
        svc = CampaignService(SimChain(w.sim), w.launcher_id, "testnet11", lambda cs: None, registry=reg)
        out = svc.distributors()
        self.assertEqual((out["this"], out["listed"], out["pins"]), (w.launcher_id.hex(), CURRENT, pins_fingerprint()))
        self.assertEqual([d["status"] for d in out["distributors"]], [CURRENT, RETIRED])
        json.dumps(out)
        bare = CampaignService(SimChain(w.sim), w.launcher_id, "testnet11", lambda cs: None).distributors()
        self.assertEqual((bare["listed"], bare["distributors"]), (None, []))

    def test_the_cli_lists_and_retires(self):
        with tempfile.TemporaryDirectory() as d:
            Registry("testnet11", [entry(1)]).save(d)
            self.assertEqual(main(["list", "--network", "testnet11", "--dir", d]), 0)
            self.assertEqual(main(["retire", "--network", "testnet11", "--dir", d, "--launcher", "01" * 32]), 0)
            self.assertEqual(Registry.load("testnet11", d).current(), [])


if __name__ == "__main__":
    unittest.main()
