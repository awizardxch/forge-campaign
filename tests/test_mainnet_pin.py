"""The site's mainnet build pin (chia-cfmm src/lib/campaigns/pins.ts) against the service's template
(owner, 2026-11-25: mainnet hosting in template mode). The site rebuilds a launch's eve coin from
its pin and refuses a quote whose message goes elsewhere, so every Config number it holds must be
distributor.Config's production default, and its guardian the treasury multisig the host's
compose.yaml names. Skipped where the site is not beside this package (the public slice)."""

import pathlib
import re
import unittest

from flpcr.distributor import DEFAULT_DELEGATE_FEE, DEFAULT_DISPUTE_BOND, Config
from flpcr.model import BLOCKS_PER_DAY, DEFAULT_FEE_BPS

ROOT = pathlib.Path(__file__).resolve().parent.parent
PINS = ROOT.parent / "chia-cfmm" / "src" / "lib" / "campaigns" / "pins.ts"
COMPOSE = ROOT.parent / "chia-cfmm" / "deploy" / "hetzner" / "compose.yaml"
TREASURY = "xch1pvp8x6qcdm20tnc2uml9tu8edga2svvfj9gjc7x64207h2xxh8ys0s53j8"


@unittest.skipUnless(PINS.exists() and COMPOSE.exists(), "the site is not beside this package")
class TestMainnetPin(unittest.TestCase):
    def setUp(self):
        self.ts = PINS.read_text(encoding="utf-8")

    def config(self) -> dict:
        body = re.search(r"export const MAINNET_CONFIG = \{(.*?)\} as const;", self.ts, re.S).group(1)
        return {k: int(v.replace("_", "")) for k, v in re.findall(r"(\w+):\s*([\d_]+)", body)}

    def test_the_site_holds_the_production_config(self):
        d = Config(bytes(32), bytes(32), bytes(32), bytes(32), bytes(32), bytes(32))
        want = {"fee_bps": DEFAULT_FEE_BPS, "dispute_blocks": d.dispute_blocks, "rotate_delay": d.rotate_delay,
                "delegate_fee": DEFAULT_DELEGATE_FEE, "min_campaign_length": d.min_campaign_length,
                "max_campaign_length": d.max_campaign_length, "max_campaigns": d.max_campaigns,
                "dispute_bond": DEFAULT_DISPUTE_BOND}
        # What mainnet's template sets over the defaults (Merkl's window, a bond of about $100).
        from flpcr.build_pin import MAINNET_TEMPLATE
        want.update(MAINNET_TEMPLATE)
        self.assertEqual(self.config(), want)
        self.assertEqual(d.rotate_delay, BLOCKS_PER_DAY)

    def test_the_guardian_is_the_treasury_on_both_sides(self):
        from flpcr.networks import decode
        ph = re.search(r"MAINNET_GUARDIAN_PH = '([0-9a-f]{64})'", self.ts).group(1)
        self.assertEqual(ph, decode(TREASURY, "mainnet").hex())
        self.assertIn(f"FLPCR_GUARDIAN_ADDRESS: ${{FLPCR_MAINNET_GUARDIAN_ADDRESS:-{TREASURY}}}",
                      COMPOSE.read_text(encoding="utf-8"))

    def test_host_keys_are_puzzle_hashes_or_unset(self):
        for name in ("MAINNET_ROUTER_PH", "MAINNET_OPERATOR_PH"):
            value = re.search(rf"export const {name} = '([0-9a-f]*)';", self.ts).group(1)
            self.assertIn(len(value), (0, 64), name)


if __name__ == "__main__":
    unittest.main()
