import unittest
from inference_grid.structured_output import parse_json_object


class StructuredTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_json_object(' \n{"x": [1, true, null]}\n'), {"x": [1, True, None]})

    def test_exact_fence(self):
        for label in ["json", ""]:
            for newline in ["\n", "\r\n"]:
                self.assertEqual(
                    parse_json_object("```" + label + newline + '{"ok":1}' + newline + "```"),
                    {"ok": 1},
                )

    def test_commentary_fences(self):
        for value in [
            "here\n```json\n{}\n```",
            "```json\n{}\n```\nthanks",
            "```json\n{}\n```\n```json\n{}\n```",
            "```python\n{}\n```",
            "```json {} ```",
            '```json\n{"x":"```"}\n```',
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_json_object(value)

    def test_nonobject_and_invalid(self):
        for value in [
            "[]",
            "null",
            "1",
            "true",
            '"x"',
            "",
            "{} {}",
            '{"x":}',
            '{"x":1,}',
            "```\n[]\n```",
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_json_object(value)

    def test_duplicate_keys(self):
        for value in ['{"x":1,"x":2}', '{"nest":{"a":1,"a":2}}', '{"x":1,"\\u0078":2}']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_json_object(value)

    def test_nonfinite(self):
        for token in ["NaN", "Infinity", "-Infinity", "1e9999", "-1e9999"]:
            with self.subTest(token=token), self.assertRaises(ValueError):
                parse_json_object('{"x":[' + token + "]}")

    def test_character_limit(self):
        self.assertEqual(parse_json_object("{}", 2), {})
        with self.assertRaises(ValueError):
            parse_json_object(" {}", 2)
        for limit in [True, False, 0, -1, 1.5, None]:
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                parse_json_object("{}", limit)
        for value in [None, b"{}", {}, 1]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_json_object(value)

    def test_nesting_errors_normalized(self):
        from unittest.mock import patch

        with patch("inference_grid.structured_output.json.loads", side_effect=RecursionError):
            with self.assertRaises(ValueError):
                parse_json_object("{}")

    def test_no_schema_assumption(self):
        self.assertEqual(parse_json_object('{"unexpected":true}'), {"unexpected": True})


if __name__ == "__main__":
    unittest.main()
