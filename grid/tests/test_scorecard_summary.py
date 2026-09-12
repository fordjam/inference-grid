import copy
import unittest

from scorecard_summary import scorecard_summary


def e(family, model, category, attempts, accepted, usage=None):
    return dict(family=family, model=model, category=category, attempts=attempts, completed=accepted, accepted=accepted, held=0, resolved=attempts - accepted, repairs=0, usage=usage or {}, usage_reported=1 if usage else 0)


class SummaryTests(unittest.TestCase):
    def test_summary(self):
        entries = [e("glm", "glm-5.3-flash", "pure_function", 8, 7, {"input": 3000, "output": 12000}), e("glm", "GLM-5.3-Flash", "tests_multi_file", 4, 3, {"input": 600000, "output": 40000}), e("kimi", "kimi-k3", "independent_review", 1, 0), e("glm", "glm-5.3-flash", "independent_review", 2, 1, {"output": 20000})]
        snap = copy.deepcopy(entries)
        out = scorecard_summary(entries)
        self.assertEqual(entries, snap)
        models = [(m["family"], m["model"], m["attempts"], m["accepted"], round(m["acceptance_rate"], 3) if m["acceptance_rate"] is not None else None, m["avg_output_tokens"]) for m in out["by_model"]]
        self.assertEqual(models, [("glm", "glm-5.3-flash", 10, 8, 0.8, 16000.0), ("glm", "GLM-5.3-Flash", 4, 3, 0.75, 40000.0), ("kimi", "kimi-k3", 1, 0, 0.0, None)])
        cats = [(c["category"], c["attempts"], c["accepted"], c["best_model"]) for c in out["by_category"]]
        self.assertEqual(cats, [("independent_review", 3, 1, "glm/glm-5.3-flash"), ("pure_function", 8, 7, "glm/glm-5.3-flash"), ("tests_multi_file", 4, 3, "glm/GLM-5.3-Flash")])
        self.assertEqual(scorecard_summary([]), {"by_model": [], "by_category": []})
        zero = scorecard_summary([e("x", "m", "c", 0, 0)])
        self.assertIsNone(zero["by_model"][0]["acceptance_rate"])
        self.assertIsNone(zero["by_category"][0]["best_model"])

    def test_refusals(self):
        for bad in ("x", [{"family": "a"}], [dict(e("a", "m", "c", 1, 1), attempts=True)], [dict(e("a", "m", "c", 1, 1), accepted=-1)]):
            with self.assertRaises(ValueError):
                scorecard_summary(bad)


if __name__ == "__main__":
    unittest.main()
