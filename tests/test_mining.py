import unittest

from cogs.mining import build_material_breakdown


class BuildMaterialBreakdownTests(unittest.TestCase):
    def test_builds_breakdown_from_total_items(self):
        seq = iter(["iron_ore", "iron_ore", "coal"])
        breakdown = build_material_breakdown(3, lambda: next(seq))
        self.assertEqual(breakdown, {"iron_ore": 2, "coal": 1})

    def test_returns_empty_breakdown_for_zero_items(self):
        self.assertEqual(build_material_breakdown(0, lambda: "iron_ore"), {})


if __name__ == "__main__":
    unittest.main()
