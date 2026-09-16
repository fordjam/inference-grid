"""The lanes-meta sidecar: loader, validator and the policy matcher (brief L8)."""

import json
import tempfile
import unittest
from pathlib import Path

from inference_grid.lanes.meta import (
    FILENAME,
    load_lane_meta,
    policy_drop,
    validate_lane_meta,
    validate_requirement,
)


def write_meta(lanes_path, document):
    path = lanes_path.parent / FILENAME
    path.write_text(document if isinstance(document, str) else json.dumps(document))
    return path


class LoaderTests(unittest.TestCase):
    def test_no_sidecar_reads_as_no_records(self):
        # The common board: the operator has tagged nothing. An absent file is the
        # unknown-everywhere reading, never an error.
        self.assertEqual(load_lane_meta("/some/board/lanes.json"), {})
        self.assertEqual(load_lane_meta(None), {})

    def test_the_sidecar_beside_lanes_json_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            lanes_path = Path(tmp) / "lanes.json"
            write_meta(
                lanes_path,
                {
                    "lanes": {
                        "go-opencode": {
                            "residency": "us",
                            "retention": "zero",
                            "retention_source": "https://example.com/zen",
                        },
                        "go": {"residency": "unknown"},
                    }
                },
            )
            self.assertEqual(
                load_lane_meta(lanes_path),
                {
                    "go-opencode": {
                        "residency": "us",
                        "retention": "zero",
                        "retention_source": "https://example.com/zen",
                    },
                    "go": {"residency": "unknown"},
                },
            )

    def test_a_malformed_sidecar_refuses_with_the_parse_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            lanes_path = Path(tmp) / "lanes.json"
            write_meta(lanes_path, "{not json")
            with self.assertRaises(ValueError) as raised:
                load_lane_meta(lanes_path)
            self.assertIn("does not parse", str(raised.exception))

    def test_the_validator_refuses_junk_shapes_and_values(self):
        for junk in (
            [],
            {},
            {"lanes": []},
            {"lanes": {}, "extra": 1},
            {"lanes": {"go": []}},
            {"lanes": {"go": {"residency": "mars"}}},
            {"lanes": {"go": {"retention": "forever"}}},
            {"lanes": {"go": {"hosting": "us"}}},  # a config key, not a sidecar key
            {"lanes": {"go": {"retention_source": ""}}},
            {"lanes": {"go": {"retention_source": 7}}},
            {"lanes": {"": {"residency": "us"}}},
        ):
            with self.assertRaises(ValueError, msg=junk):
                validate_lane_meta(junk)
        # Any subset of the three keys is a record; a source naming a vendor's page is text.
        self.assertEqual(
            validate_lane_meta(
                {"lanes": {"go": {"retention": "days", "retention_source": "vendor FAQ"}}}
            ),
            {"lanes": {"go": {"retention": "days", "retention_source": "vendor FAQ"}}},
        )


class RequirementTests(unittest.TestCase):
    def test_the_us_zero_requirement_validates(self):
        self.assertEqual(
            validate_requirement({"residency": ["us", "eu"], "retention": ["zero"]}),
            {"residency": ["us", "eu"], "retention": ["zero"]},
        )

    def test_junk_requirements_refuse(self):
        for junk in (
            {},
            [],
            {"hosting": ["us"]},  # not a sidecar key
            {"residency": []},
            {"residency": "us"},  # not a list
            {"residency": [None]},
            {"residency": [""]},
            {"residency": ["mars"]},
            # unknown never satisfies, so it can never appear among the allowed values.
            {"retention": ["zero", "unknown"]},
        ):
            with self.assertRaises(ValueError, msg=junk):
                validate_requirement(junk)


class PolicyDropTests(unittest.TestCase):
    REQUIRE = {"residency": ["us", "eu"], "retention": ["zero"]}

    def test_a_tagged_lane_satisfies_and_yields_no_row(self):
        self.assertIsNone(policy_drop("go", {"residency": "us", "retention": "zero"}, self.REQUIRE))
        self.assertIsNone(policy_drop("go", {"residency": "eu", "retention": "zero"}, self.REQUIRE))

    def test_unknown_never_satisfies_and_the_row_names_the_key(self):
        self.assertEqual(
            policy_drop("go", {"residency": "unknown", "retention": "zero"}, self.REQUIRE),
            {
                "lane": "go",
                "reason": "lane_policy",
                "detail": "residency unknown not in [us, eu]",
            },
        )

    def test_an_untagged_lane_reads_unknown(self):
        # A lane the sidecar does not name is refused on the first listed key, not
        # silently offered.
        self.assertEqual(
            policy_drop("go", {}, self.REQUIRE),
            {
                "lane": "go",
                "reason": "lane_policy",
                "detail": "residency unknown not in [us, eu]",
            },
        )
        # A record carrying only one of the two required keys fails the other.
        self.assertEqual(
            policy_drop("go", {"residency": "us"}, self.REQUIRE),
            {
                "lane": "go",
                "reason": "lane_policy",
                "detail": "retention unknown not in [zero]",
            },
        )

    def test_the_first_failing_listed_key_is_named(self):
        self.assertEqual(
            policy_drop("go", {"residency": "us", "retention": "days"}, self.REQUIRE)["detail"],
            "retention days not in [zero]",
        )

    def test_a_days_retention_drops_where_zero_is_required(self):
        row = policy_drop("go", {"residency": "us", "retention": "days"}, self.REQUIRE)
        self.assertEqual((row["lane"], row["reason"]), ("go", "lane_policy"))


if __name__ == "__main__":
    unittest.main()
