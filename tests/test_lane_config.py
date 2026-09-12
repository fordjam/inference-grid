import copy
import unittest

from inference_grid.lanes.config import validate_lane_config


def lane(**kw):
    base = dict(
        provider="opencode",
        family="glm",
        model="glm-5.3-flash",
        kind="go_http",
        credential_path="/private/key.json",
        executable=None,
        plan_units={"five_hour": 12, "weekly": 30},
        window=None,
        max_concurrency=1,
        wall_seconds=200,
        categories=["pure_function"],
    )
    base.update(kw)
    return base


class ConfigTests(unittest.TestCase):
    def test_valid_copy(self):
        raw = {
            "lanes": {
                "go": lane(),
                "zcode": lane(
                    kind="zcode_cli",
                    credential_path=None,
                    executable="/opt/homebrew/bin/node",
                    window="zai_flash",
                    plan_units={},
                    categories=["tests_multi_file", "pure_function"],
                ),
            }
        }
        snapshot = copy.deepcopy(raw)
        out = validate_lane_config(raw)
        self.assertEqual(out, raw)
        self.assertEqual(raw, snapshot)
        self.assertIsNot(out["lanes"]["go"]["categories"], raw["lanes"]["go"]["categories"])
        self.assertIsNot(out["lanes"]["go"]["plan_units"], raw["lanes"]["go"]["plan_units"])

    def test_refusals(self):
        bad = [
            {},
            {"lanes": {}},
            {"lanes": {}, "x": 1},
            {"lanes": {"Go": lane()}},
            {"lanes": {"": lane()}},
            {"lanes": {"go": "x"}},
            {"lanes": {"go": lane(kind="http")}},
            {"lanes": {"go": lane(credential_path="key.json")}},
            {"lanes": {"go": lane(executable="node")}},
            {"lanes": {"go": lane(plan_units={"w": 0})}},
            {"lanes": {"go": lane(plan_units={"w": True})}},
            {"lanes": {"go": lane(plan_units={"": 1})}},
            {"lanes": {"go": lane(window="night")}},
            {"lanes": {"go": lane(max_concurrency=0)}},
            {"lanes": {"go": lane(max_concurrency=True)}},
            {"lanes": {"go": lane(wall_seconds=10)}},
            {"lanes": {"go": lane(categories=[])}},
            {"lanes": {"go": lane(categories=["a", "a"])}},
            {"lanes": {"go": dict(lane(), extra=1)}},
            {"lanes": {"go": {k: v for k, v in lane().items() if k != "model"}}},
            {"lanes": {"go": lane(model="")}},
        ]
        for raw in bad:
            with self.assertRaises(ValueError, msg=str(raw)[:120]) as ctx:
                validate_lane_config(raw)
            self.assertRegex(str(ctx.exception), r"^[^:]*:")


if __name__ == "__main__":
    unittest.main()
