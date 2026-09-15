import copy
import json
import unittest

from test_board_runner import make_task, world  # noqa: F401 -- binds the world fixture here

from inference_grid.board import runner
from inference_grid.lanes.route import blended_row, max_tokens_cap, route

LANES = {
    "go": {
        "family": "glm",
        "model": "glm-5.3-flash",
        "categories": ["pure_function"],
        "window_active": None,
    },
    "zcode": {
        "family": "glm",
        "model": "GLM-5.3-Flash",
        "categories": ["pure_function", "tests_multi_file"],
        "window_active": True,
    },
    "kimi": {
        "family": "kimi",
        "model": "kimi-k3",
        "categories": ["independent_review"],
        "window_active": None,
    },
    "claude": {
        "family": "claude",
        "model": "claude-opus-4",
        "categories": ["independent_review"],
        "window_active": None,
        "explicit_only": True,
    },
}
READY = {k: {"state": "ready"} for k in LANES}
# A 16 K-token packet: 65536 bytes at the 4 bytes-per-token estimate.
BIG_PACKET = 65536


def task(**overrides):
    base = {
        "category": "pure_function",
        "author_family": None,
        "budget": {"wall_seconds": 60, "output_bytes": 100000, "thinking_tokens": None},
    }
    base.update(overrides)
    return base


class DefaultingTests(unittest.TestCase):
    def test_absent_or_empty_lanes_default_to_the_category(self):
        for lanes_key in (None, []):
            raw = task()
            if lanes_key is not None:
                raw["lanes"] = lanes_key
            out = route(raw, LANES, READY, [], [], 0, 0)
            # Both category lanes tie at 1/2; lane id breaks the tie.
            self.assertEqual((out["lane"], out["candidates"]), ("go", ["go", "zcode"]))
            self.assertEqual((out["score"], out["reason"], out["dropped"]), (0.5, "selected", []))

    def test_an_explicit_list_still_restricts(self):
        out = route(task(lanes=["zcode"]), LANES, READY, [], [], 0, 0)
        self.assertEqual((out["lane"], out["candidates"], out["score"]), ("zcode", ["zcode"], 0.5))
        # A listed lane that does not declare the category is simply not offered,
        # but it still counts as a candidate: select_lane drops it, not route.
        out = route(task(lanes=["zcode", "kimi"]), LANES, READY, [], [], 0, 0)
        self.assertEqual((out["lane"], out["candidates"]), ("zcode", ["kimi", "zcode"]))

    def test_explicit_only_lanes_are_never_defaulted_in(self):
        out = route(task(category="independent_review"), LANES, READY, [], [], 0, 0)
        self.assertEqual((out["lane"], out["candidates"]), ("kimi", ["kimi"]))
        # ... but an author naming one takes it anyway.
        out = route(
            task(category="independent_review", lanes=["claude"]), LANES, READY, [], [], 0, 0
        )
        self.assertEqual((out["lane"], out["candidates"]), ("claude", ["claude"]))

    def test_inputs_are_not_mutated(self):
        frozen = (task(lanes=["go"]), LANES, READY, [{"attempts": 8, "accepted": 7}], [])
        snapshot = copy.deepcopy(frozen)
        route(frozen[0], frozen[1], frozen[2], frozen[3], frozen[4], 0, 0)
        self.assertEqual(frozen, snapshot)


class BudgetTests(unittest.TestCase):
    def test_the_go_policy_caps_an_unbudgeted_lane_at_16k(self):
        self.assertEqual(max_tokens_cap({}, None), 16000)
        self.assertEqual(max_tokens_cap({}, 2000), 10000)  # 3 * 2000 + 4000
        self.assertEqual(max_tokens_cap({}, 6000), 22000)
        self.assertEqual(max_tokens_cap({"max_tokens": 9000}, 6000), 9000)

    def test_a_16k_packet_misses_a_10k_cap_and_fits_22k(self):
        budget = {"wall_seconds": 60, "output_bytes": 100000, "thinking_tokens": 2000}
        out = route(task(lanes=["go"], budget=budget), LANES, READY, [], [], 0, BIG_PACKET)
        self.assertEqual(
            (out["lane"], out["reason"], out["candidates"]), (None, "budget_unfit", [])
        )
        self.assertEqual(
            out["dropped"],
            [
                {
                    "lane": "go",
                    "reason": "budget_unfit",
                    "detail": "prompt ~16384 tokens exceeds max_tokens cap 10000",
                }
            ],
        )
        budget = dict(budget, thinking_tokens=6000)  # 3 * 6000 + 4000 = 22000
        out = route(task(lanes=["go"], budget=budget), LANES, READY, [], [], 0, BIG_PACKET)
        self.assertEqual((out["lane"], out["dropped"]), ("go", []))
        # Under the default candidate set both pure_function lanes miss the 10k cap.
        out = route(
            task(budget=dict(budget, thinking_tokens=2000)), LANES, READY, [], [], 0, BIG_PACKET
        )
        self.assertEqual([d["lane"] for d in out["dropped"]], ["go", "zcode"])

    def test_a_lane_view_max_tokens_figure_overrides_the_policy(self):
        lanes = dict(LANES, go=dict(LANES["go"], max_tokens=22000))
        out = route(task(lanes=["go"]), lanes, READY, [], [], 0, BIG_PACKET)
        self.assertEqual((out["lane"], out["dropped"]), ("go", []))
        lanes = dict(LANES, go=dict(LANES["go"], max_tokens=10000))
        out = route(task(lanes=["go"]), lanes, READY, [], [], 0, BIG_PACKET)
        self.assertEqual((out["lane"], out["reason"]), (None, "budget_unfit"))

    def test_a_context_cap_filters_independently_of_max_tokens(self):
        lanes = dict(LANES, go=dict(LANES["go"], max_tokens=22000, context=8000))
        out = route(task(lanes=["go"]), lanes, READY, [], [], 0, BIG_PACKET)
        self.assertEqual(out["dropped"][0]["detail"], "prompt ~16384 tokens exceeds context 8000")
        lanes = dict(LANES, go=dict(LANES["go"], max_tokens=22000, context=16384))
        self.assertEqual(
            route(task(lanes=["go"]), lanes, READY, [], [], 0, BIG_PACKET)["lane"], "go"
        )

    def test_readiness_and_family_reasons_survive_the_pass_through(self):
        cold = {k: {"state": "stale"} for k in LANES}
        out = route(task(), LANES, cold, [], [], 0, 0)
        self.assertEqual((out["lane"], out["reason"]), (None, "no_ready_lane"))
        out = route(
            task(category="independent_review", author_family="kimi"), LANES, READY, [], [], 0, 0
        )
        self.assertEqual(out["reason"], "no_independent_family")
        with self.assertRaises(ValueError):
            route(task(), LANES, {}, [], [], 0, 0)


class BlendTests(unittest.TestCase):
    SCORECARD = [
        {
            "family": "glm",
            "model": "glm-5.3-flash",
            "category": "independent_review",
            "attempts": 8,
            "accepted": 7,
        },
        {
            "family": "kimi",
            "model": "kimi-k3",
            "category": "independent_review",
            "attempts": 4,
            "accepted": 4,
        },
    ]

    def test_the_reshaped_row_scores_the_blend_exactly(self):
        # 0.5 * 8/10 + 0.5 * 3/4 = 31/40: attempts 38, accepted 30.
        row = blended_row(
            LANES["go"],
            "independent_review",
            self.SCORECARD,
            [{"family": "glm", "model": "glm-5.3-flash", "cases": 4, "accepted": 3}],
        )
        self.assertEqual((row["attempts"], row["accepted"]), (38, 30))
        self.assertEqual((row["accepted"] + 1) / (row["attempts"] + 2), 0.775)

    def test_no_calibration_keeps_the_original_row(self):
        row = blended_row(LANES["go"], "independent_review", self.SCORECARD, [])
        self.assertEqual((row["attempts"], row["accepted"]), (8, 7))
        self.assertIsNone(blended_row(LANES["kimi"], "independent_review", [], []))

    def test_calibration_without_a_scorecard_row_blends_with_the_default_half(self):
        row = blended_row(
            LANES["kimi"],
            "independent_review",
            [],
            [{"family": "kimi", "model": "kimi-k3", "cases": 2, "accepted": 1}],
        )
        self.assertEqual((row["attempts"], row["accepted"]), (0, 0))  # 1/2 * 1/2 + 1/2 * 1/2

    def test_a_strong_record_blends_above_its_acceptance(self):
        # kimi's Laplace is 5/6; a perfect calibration run lifts the blend to 11/12.
        row = blended_row(
            LANES["kimi"],
            "independent_review",
            self.SCORECARD,
            [{"family": "kimi", "model": "kimi-k3", "cases": 6, "accepted": 6}],
        )
        self.assertEqual((row["accepted"] + 1) / (row["attempts"] + 2), 11 / 12)

    def test_recall_steers_selection(self):
        lanes = {
            "go": dict(LANES["go"], categories=["pure_function", "independent_review"]),
            "kimi": LANES["kimi"],
        }
        ready = {"go": READY["go"], "kimi": READY["kimi"]}
        task_dict = task(category="independent_review", lanes=["go", "kimi"])
        # kimi's plain acceptance (5/6) beats go's default 1/2...
        self.assertEqual(route(task_dict, lanes, ready, self.SCORECARD, [], 0, 0)["lane"], "kimi")
        calibration = [
            {"family": "glm", "model": "glm-5.3-flash", "cases": 4, "accepted": 4},
            {"family": "kimi", "model": "kimi-k3", "cases": 4, "accepted": 0},
        ]
        out = route(task_dict, lanes, ready, self.SCORECARD, calibration, 0, 0)
        # ... but a perfect calibration record lifts go to 9/10 (0.5 * 8/10 + 0.5 * 1)
        # while kimi's empty one drags it to 5/12.
        self.assertEqual((out["lane"], out["score"]), ("go", 0.9))


def test_the_tick_reports_dropped_lanes(request):
    w = request.getfixturevalue("world")
    (w["project"] / "brief-big.txt").write_text("x" * BIG_PACKET + "\n")
    (w["board"] / "copy-big.json").write_text(json.dumps(make_task("copy-big", "brief-big.txt")))
    plan = runner.tick(
        w["board"],
        w["project"],
        w["ledger"],
        w["lanes"],
        w["lanes_path"],
        {"go": w["account"]},
        w["packets"],
        now=0,
        dry_run=True,
    )
    entry = plan["plan"][0]
    # The unbudgeted go policy caps at 16k: the 16k-token packet misses it, and the
    # plan names the lane and the reason instead of a generic no-ready-lane.
    assert entry["task"] == "copy-big"
    assert (entry["lane"], entry["reason"], entry["candidates"]) == (None, "budget_unfit", [])
    assert entry["dropped"][0]["lane"] == "go"
    assert entry["dropped"][0]["reason"] == "budget_unfit"
    # The dry run touched nothing: the lane was never recorded, so it stays stale.
    assert plan["readiness"]["go"]["state"] == "stale"


if __name__ == "__main__":
    unittest.main()
