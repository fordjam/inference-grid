import copy
import unittest

from inference_grid.lanes.select import select_lane

LANES = {
    "go": {
        "family": "glm",
        "model": "glm-5.3-flash",
        "categories": ["pure_function", "independent_review"],
        "window_active": None,
    },
    "zcode": {
        "family": "glm",
        "model": "GLM-5.3-Flash",
        "categories": ["tests_multi_file", "pure_function"],
        "window_active": True,
    },
    "goat": {
        "family": "glm",
        "model": "z-ai/glm-5.3-flash",
        "categories": ["tests_multi_file"],
        "window_active": None,
    },
    "zai": {
        "family": "glm",
        "model": "glm-5.3-flash-headless",
        "categories": ["independent_review"],
        "window_active": None,
    },
    "kimi": {
        "family": "kimi",
        "model": "kimi-k3",
        "categories": ["independent_review"],
        "window_active": None,
    },
}
READY = {k: {"state": "ready"} for k in LANES}
CARD = [
    {
        "family": "glm",
        "model": "glm-5.3-flash",
        "category": "pure_function",
        "attempts": 8,
        "accepted": 7,
    },
    {
        "family": "glm",
        "model": "GLM-5.3-Flash",
        "category": "pure_function",
        "attempts": 0,
        "accepted": 0,
    },
    {
        "family": "glm",
        "model": "z-ai/glm-5.3-flash",
        "category": "tests_multi_file",
        "attempts": 2,
        "accepted": 2,
    },
    {
        "family": "kimi",
        "model": "kimi-k3",
        "category": "independent_review",
        "attempts": 1,
        "accepted": 0,
    },
]


class SelectTests(unittest.TestCase):
    def test_scoring_and_ties(self):
        out = select_lane({"category": "pure_function"}, LANES, READY, CARD, 0)
        self.assertEqual(out["candidates"], ["go", "zcode"])
        self.assertEqual(
            (out["lane"], round(out["score"], 4), out["reason"]), ("go", 0.8, "selected")
        )
        out = select_lane({"category": "tests_multi_file"}, LANES, READY, CARD, 0)
        self.assertEqual(out["lane"], "goat")  # 3/4 beats zcode's 1/2
        tie = [
            {
                "family": "glm",
                "model": "z-ai/glm-5.3-flash",
                "category": "tests_multi_file",
                "attempts": 2,
                "accepted": 1,
            }
        ]
        out = select_lane({"category": "tests_multi_file"}, LANES, READY, tie, 0)
        self.assertEqual(out["lane"], "zcode")  # both 0.5; zcode has fewer attempts
        same = [
            {
                "family": "glm",
                "model": "z-ai/glm-5.3-flash",
                "category": "tests_multi_file",
                "attempts": 0,
                "accepted": 0,
            }
        ]
        self.assertEqual(
            select_lane({"category": "tests_multi_file"}, LANES, READY, same, 0)["lane"], "goat"
        )  # id order

    def test_independent_family_and_windows(self):
        out = select_lane(
            {"category": "independent_review", "author_family": "glm"}, LANES, READY, CARD, 0
        )
        self.assertEqual((out["lane"], out["candidates"]), ("kimi", ["kimi"]))
        ready = dict(READY, kimi={"state": "blocked"})
        out = select_lane(
            {"category": "independent_review", "author_family": "glm"}, LANES, ready, CARD, 0
        )
        self.assertEqual((out["lane"], out["reason"]), (None, "no_independent_family"))
        lanes = copy.deepcopy(LANES)
        lanes["zcode"]["window_active"] = False
        out = select_lane(
            {"category": "tests_multi_file"},
            lanes,
            dict(READY, goat={"state": "exhausted"}),
            CARD,
            0,
        )
        self.assertEqual(
            (out["lane"], out["reason"], out["candidates"]), (None, "window_closed", [])
        )

    def test_no_lane_reasons_and_refusals(self):
        self.assertEqual(
            select_lane({"category": "x"}, LANES, READY, CARD, 0)["reason"], "no_lane_for_category"
        )
        cold = {k: {"state": "stale"} for k in LANES}
        self.assertEqual(
            select_lane({"category": "pure_function"}, LANES, cold, CARD, 0)["reason"],
            "no_ready_lane",
        )
        snapshot = copy.deepcopy((LANES, READY, CARD))
        select_lane({"category": "pure_function"}, LANES, READY, CARD, 0)
        self.assertEqual((LANES, READY, CARD), snapshot)
        for bad in ({"category": ""}, {}, {"category": 3}):
            with self.assertRaises(ValueError):
                select_lane(bad, LANES, READY, CARD, 0)
        with self.assertRaises(ValueError):
            select_lane({"category": "pure_function"}, LANES, {"go": {"state": "ready"}}, CARD, 0)
        with self.assertRaises(ValueError):
            select_lane({"category": "pure_function"}, "lanes", READY, CARD, 0)


if __name__ == "__main__":
    unittest.main()
