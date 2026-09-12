import unittest

from inference_grid.aliases import canonical_account


class TestCanonicalAccount(unittest.TestCase):
    def test_no_alias(self):
        self.assertEqual(canonical_account("acc1", {}), "acc1")
        self.assertEqual(canonical_account("acc1", {"other": "x"}), "acc1")

    def test_multi_hop(self):
        aliases = {"a": "b", "b": "c", "c": "terminal"}
        self.assertEqual(canonical_account("a", aliases), "terminal")

    def test_cycle(self):
        with self.assertRaises(ValueError):
            canonical_account("a", {"a": "b", "b": "a"})
        with self.assertRaises(ValueError):
            canonical_account("a", {"a": "a"})

    def test_malformed(self):
        cases = [
            ("", {}),
            ("  ", {}),
            (123, {}),
            ("a", None),
            ("a", ["a", "b"]),
            ("a", {"": "b"}),
            ("a", {"  ": "b"}),
            ("a", {1: "b"}),
            ("a", {"b": ""}),
            ("a", {"b": "  "}),
            ("a", {"b": 5}),
        ]
        for name, aliases in cases:
            with self.subTest(name=name, aliases=aliases):
                with self.assertRaises(ValueError):
                    canonical_account(name, aliases)

    def test_unrelated_mapping(self):
        aliases = {"x": "y", "y": "z"}
        self.assertEqual(canonical_account("main", aliases), "main")
        self.assertEqual(canonical_account("z", aliases), "z")


if __name__ == "__main__":
    unittest.main()
