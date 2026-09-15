import copy
import json
import time
import unittest

from test_board_runner import make_review_task, make_task, ready_record, world  # noqa: F401 -- binds the world fixture here

from inference_grid.board import runner
from inference_grid.lanes.route import blended_row, lane_tier, max_tokens_cap, route, wanted_tier

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
            # Both category lanes tie at 1/2; lane id breaks the tie. Each candidate
            # row carries the cap the lane would run under (unbudgeted go policy: 16k).
            self.assertEqual(
                (out["lane"], out["candidates"]),
                ("go", [{"lane": "go", "cap": 16000}, {"lane": "zcode", "cap": 16000}]),
            )
            self.assertEqual((out["score"], out["reason"], out["dropped"]), (0.5, "selected", []))

    def test_an_explicit_list_still_restricts(self):
        out = route(task(lanes=["zcode"]), LANES, READY, [], [], 0, 0)
        self.assertEqual(
            (out["lane"], out["candidates"], out["score"]),
            ("zcode", [{"lane": "zcode", "cap": 16000}], 0.5),
        )
        # A listed lane that does not declare the category is simply not offered,
        # but it still counts as a candidate: select_lane drops it, not route.
        out = route(task(lanes=["zcode", "kimi"]), LANES, READY, [], [], 0, 0)
        self.assertEqual(
            (out["lane"], out["candidates"]),
            ("zcode", [{"lane": "kimi", "cap": 16000}, {"lane": "zcode", "cap": 16000}]),
        )

    def test_explicit_only_lanes_are_never_defaulted_in(self):
        out = route(task(category="independent_review"), LANES, READY, [], [], 0, 0)
        self.assertEqual(
            (out["lane"], out["candidates"]), ("kimi", [{"lane": "kimi", "cap": 16000}])
        )
        # ... but an author naming one takes it anyway.
        out = route(
            task(category="independent_review", lanes=["claude"]), LANES, READY, [], [], 0, 0
        )
        self.assertEqual(
            (out["lane"], out["candidates"]), ("claude", [{"lane": "claude", "cap": 16000}])
        )

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
        # The refusal is explained in tokens: the prompt estimate and the cap it missed.
        self.assertEqual(
            out["dropped"],
            [
                {
                    "lane": "go",
                    "reason": "budget_unfit",
                    "detail": "output need ~12192 tokens (prompt/2 + 4000) exceeds max_tokens cap 10000",
                    "prompt": 16384,
                    "cap": 10000,
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
        # The row names the limit that actually refused: the context window here.
        self.assertEqual((out["dropped"][0]["prompt"], out["dropped"][0]["cap"]), (16384, 8000))
        lanes = dict(LANES, go=dict(LANES["go"], max_tokens=22000, context=16384))
        self.assertEqual(
            route(task(lanes=["go"]), lanes, READY, [], [], 0, BIG_PACKET)["lane"], "go"
        )

    def test_a_lane_whose_cap_is_below_the_packet_need_is_refused_with_both_numbers(self):
        # The lane view's own figure is the cap; the row carries it beside the prompt
        # estimate, so a dry run can explain the refusal without reading the lane file.
        lanes = dict(LANES, kimi=dict(LANES["kimi"], max_tokens=9000))
        out = route(
            task(category="independent_review"),
            lanes,
            READY,
            [],
            [],
            0,
            40008,  # ceil(40008 / 4) = 10002 tokens: need 10002 // 2 + 4000 = 9001, one over the cap
        )
        self.assertEqual((out["lane"], out["reason"]), (None, "budget_unfit"))
        self.assertEqual(out["dropped"][0]["prompt"], 10002)
        self.assertEqual(out["dropped"][0]["cap"], 9000)

    def test_a_lane_view_max_tokens_figure_is_reported_on_the_candidate_row(self):
        lanes = dict(LANES, kimi=dict(LANES["kimi"], max_tokens=31000))
        out = route(task(category="independent_review"), lanes, READY, [], [], 0, 0)
        self.assertEqual(out["candidates"], [{"lane": "kimi", "cap": 31000}])

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


class TierTests(unittest.TestCase):
    """The lane record's tier, read by route (J2): plan | build | review."""

    # A lane that declares another tier than the task asks for loses even when its
    # evidence is the better of the two: 8/8 acceptance for go against nothing for kimi.
    REVIEW_SCORECARD = [
        {
            "family": "glm",
            "model": "glm-5.3-flash",
            "category": "independent_review",
            "attempts": 8,
            "accepted": 8,
        }
    ]

    def test_the_lane_of_the_task_tier_wins_and_the_other_is_dropped(self):
        lanes = {
            "go": dict(
                LANES["go"], categories=["pure_function", "independent_review"], tier="build"
            ),
            "kimi": dict(LANES["kimi"], tier="review"),
        }
        ready = {k: {"state": "ready"} for k in lanes}
        out = route(
            task(category="independent_review", lanes=["go", "kimi"]),
            lanes,
            ready,
            self.REVIEW_SCORECARD,
            [],
            0,
            0,
        )
        # go's Laplace 9/10 would have won the selection; the task's tier decides first,
        # so the only offered lane is the review one, at its default score.
        self.assertEqual(
            (out["lane"], out["score"], out["candidates"]),
            ("kimi", 0.5, [{"lane": "kimi", "cap": 16000}]),
        )
        self.assertEqual(out["tier"], "review")
        self.assertEqual(
            [(d["lane"], d["reason"], d["detail"]) for d in out["dropped"]],
            [("go", "tier_mismatch", "lane tier build, task wants review")],
        )

    def test_ties_within_the_task_tier_break_on_the_existing_score(self):
        # Both lanes declare review: the tier filter has nothing to drop, and the
        # evidence decides — go's 9/10 over kimi's default 1/2.
        lanes = {
            "go": dict(
                LANES["go"], categories=["pure_function", "independent_review"], tier="review"
            ),
            "kimi": dict(LANES["kimi"], tier="review"),
            "zcode": dict(LANES["zcode"], categories=["pure_function"], tier="build"),
        }
        ready = {k: {"state": "ready"} for k in lanes}
        out = route(
            task(category="independent_review", lanes=["go", "kimi"]),
            lanes,
            ready,
            self.REVIEW_SCORECARD,
            [],
            0,
            0,
        )
        self.assertEqual((out["lane"], out["score"], out["dropped"]), ("go", 0.9, []))

    def test_a_plan_task_prefers_the_lane_marked_tier_plan(self):
        lanes = {
            "sota": dict(LANES["go"], categories=["plan"], tier="plan"),
            "go": dict(LANES["go"], categories=["plan"], tier="build"),
        }
        ready = {k: {"state": "ready"} for k in lanes}
        out = route(task(category="plan", lanes=["sota", "go"]), lanes, ready, [], [], 0, 0)
        self.assertEqual(
            (out["lane"], out["tier"], [d["reason"] for d in out["dropped"]]),
            ("sota", "plan", ["tier_mismatch"]),
        )

    def test_an_absent_or_non_string_tier_is_build(self):
        # The packaged lanes.json key set cannot carry the key, so an absent tier is the
        # workhorse default — the same read runner.lane_view performs.
        for lane_dict in (
            LANES["go"],
            dict(LANES["go"], tier=None),
            dict(LANES["go"], tier=1),
        ):
            self.assertEqual(lane_tier(lane_dict), "build")
        self.assertEqual(lane_tier(dict(LANES["go"], tier="review")), "review")
        # ... and the category's tier: plan → plan, review → review, else build.
        self.assertEqual(
            [wanted_tier(c) for c in ("plan", "independent_review", "packet", "canary", None)],
            ["plan", "review", "build", "build", "build"],
        )

    def test_a_build_task_drops_a_review_tier_lane(self):
        lanes = {
            "go": dict(LANES["go"], tier="review"),
            "zcode": LANES["zcode"],  # absent tier → build
        }
        ready = {k: {"state": "ready"} for k in lanes}
        out = route(task(lanes=["go", "zcode"]), lanes, ready, [], [], 0, 0)
        self.assertEqual(
            (out["lane"], out["candidates"]), ("zcode", [{"lane": "zcode", "cap": 16000}])
        )
        self.assertEqual(out["dropped"][0]["reason"], "tier_mismatch")
        self.assertEqual(out["tier"], "build")

    def test_no_lane_of_the_task_tier_leaves_every_lane_offered(self):
        # A board whose records predate the key, or a category with no SOTA lane yet:
        # the filter never empties the offer, so routing is what it always was.
        lanes = {"go": LANES["go"], "kimi": LANES["kimi"]}
        ready = {k: {"state": "ready"} for k in lanes}
        out = route(
            task(category="independent_review", lanes=["go", "kimi"]), lanes, ready, [], [], 0, 0
        )
        self.assertEqual(
            (out["lane"], out["dropped"], out["tier"]),
            ("kimi", [], "review"),
        )


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
    # 16k tokens of prompt needs ~12.2k of output room; the unbudgeted policy's 16k cap
    # holds that, so the packet must be bigger to miss it: 30k tokens -> need ~19k > 16k.
    (w["project"] / "brief-big.txt").write_text("x" * (BIG_PACKET * 2) + "\n")
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
    # The refusal is explained in tokens, in the plan row itself. The packet estimate
    # covers the brief and every staged input (brief-big.txt plus mod.py, 65547 bytes).
    assert entry["dropped"][0]["prompt"] == 32771
    assert entry["dropped"][0]["cap"] == 16000
    # The dry run touched nothing: the lane was never recorded, so it stays stale.
    assert plan["readiness"]["go"]["state"] == "stale"


def test_the_plan_row_carries_the_cap_of_each_candidate(request):
    w = request.getfixturevalue("world")
    (w["board"] / "copy-ok.json").write_text(json.dumps(make_task("copy-ok", "brief.txt")))
    now = time.time()
    w["ledger"].record_lane("go", ready_record(now))
    plan = runner.tick(
        w["board"],
        w["project"],
        w["ledger"],
        w["lanes"],
        w["lanes_path"],
        {"go": w["account"]},
        w["packets"],
        now=now,
        dry_run=True,
    )
    entry = plan["plan"][0]
    assert (entry["task"], entry["lane"], entry["reason"]) == ("copy-ok", "go", "selected")
    # The offered candidate carries the cap it would run under — the dry run shows the
    # request size the lane will be asked for before anything is dispatched.
    assert entry["candidates"] == [{"lane": "go", "cap": 16000}]


def test_the_dry_run_explains_a_tier_mismatch(request):
    w = request.getfixturevalue("world")
    # A review task naming a workhorse and a SOTA lane: the build lane is not offered for
    # it, and the plan row names the tier each declares.
    (w["board"] / "review-tier.json").write_text(
        json.dumps(
            dict(
                make_review_task("review-tier", "brief-approve.txt", author_family="glm"),
                lanes=["go", "sota"],
            )
        )
    )
    lanes = dict(
        w["lanes"],
        sota=dict(w["lanes"]["go"], family="kimi", model="kimi-k3", tier="review"),
    )
    now = time.time()
    # The review lane's model has its evidence rows (the fixture seeds them); the account
    # only has to be configured and named for readiness.
    w["ledger"].configure_account(
        "sota-acct",
        1,
        {"five_hour": 10, "weekly": 20},
        now + 600,
        ["kimi-k3"],
        ["sota-alias"],
    )
    w["ledger"].record_lane("go", ready_record(now))
    w["ledger"].record_lane("sota", dict(ready_record(now), provider="sota"))
    plan = runner.tick(
        w["board"],
        w["project"],
        w["ledger"],
        lanes,
        w["lanes_path"],
        {"go": w["account"], "sota": "sota-alias"},
        w["packets"],
        now=now,
        dry_run=True,
    )
    entry = next(row for row in plan["plan"] if row["task"] == "review-tier")
    assert (entry["lane"], entry["candidates"]) == ("sota", [{"lane": "sota", "cap": 16000}])
    assert entry["dropped"] == [
        {"lane": "go", "reason": "tier_mismatch", "detail": "lane tier build, task wants review"}
    ]


if __name__ == "__main__":
    unittest.main()


def test_the_tick_fits_the_packet_against_the_task_budget(request):
    """A budgeted task is measured against REASONING_HEADROOM * thinking + allowance,
    not the unbudgeted 16k cap: the packet that misses the unbudgeted policy fits
    once the task carries a 10k thinking budget (cap 34k)."""
    w = request.getfixturevalue("world")
    (w["project"] / "brief-big.txt").write_text("x" * (BIG_PACKET * 2) + "\n")
    task = make_task("copy-budgeted", "brief-big.txt")
    task["budget"]["thinking_tokens"] = 10000
    (w["board"] / "copy-budgeted.json").write_text(json.dumps(task))
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
    assert entry["task"] == "copy-budgeted"
    assert entry["dropped"] == []
    assert entry["candidates"] == [{"lane": "go", "cap": 34000}]
