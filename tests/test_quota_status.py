import unittest
from inference_grid.quota_status import classify_quota_status


class StatusTests(unittest.TestCase):
    def test_success_only_validates(self):
        self.assertEqual(classify_quota_status(200), "validate")

    def test_quota(self):
        self.assertEqual(classify_quota_status(429), "cooldown")

    def test_auth(self):
        for n in [401, 403]:
            self.assertEqual(classify_quota_status(n), "auth_required")

    def test_server_range(self):
        for n in range(500, 600):
            self.assertEqual(classify_quota_status(n), "transient_error")

    def test_other_http(self):
        for n in range(100, 500):
            if n not in [200, 401, 403, 429]:
                self.assertEqual(classify_quota_status(n), "http_error")

    def test_invalid(self):
        for n in [True, False, None, "200", 200.0, 99, 600, float("nan"), {}, []]:
            with self.subTest(n=n), self.assertRaises(ValueError):
                classify_quota_status(n)


if __name__ == "__main__":
    unittest.main()
