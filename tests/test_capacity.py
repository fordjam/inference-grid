from inference_grid.capacity import project


def test_projection_strips_credentials_and_marks_bad_numbers_unknown():
    raw = {
        "accounts": [
            {
                "provider": "clinepass",
                "observed_at": "2026-09-12T12:00:00Z",
                "status": "ok",
                "token": "secret",
                "windows": [{"id": "weekly", "used_percent": float("nan"), "credential": "secret"}],
            }
        ]
    }
    result = project(raw)
    assert "secret" not in str(result)
    assert result["accounts"][0]["windows"][0]["used_percent"] is None


def test_newest_reading_wins_and_failure_replaces_old_success():
    old = {
        "provider": "claude",
        "observed_at": "2026-09-11T01:00:00Z",
        "status": "ok",
        "windows": [],
    }
    new = {
        "provider": "claude",
        "observed_at": "2026-09-12T01:00:00Z",
        "status": "unknown",
        "windows": [],
    }
    assert project({"accounts": [old]}, [new])["accounts"][0]["status"] == "unknown"
    assert project({"accounts": [new]}, [old])["accounts"][0]["status"] == "unknown"


def test_read_only_projection_does_not_expose_arbitrary_fields():
    result = project(
        {
            "accounts": [{"provider": "unrecognized", "windows": []}],
            "attempts": [{"task": "demo", "secret": "no", "status": "completed"}],
        }
    )
    assert result["accounts"] == []
    assert "secret" not in result["attempts"][0]


def test_overlay_dict_supplies_zai_account_and_replaces_attempts():
    raw = {
        "accounts": [],
        "attempts": [{"task": "Unnamed claim", "provider": "claude", "status": "board"}],
    }
    overlay = {
        "accounts": [
            {
                "provider": "zai",
                "observed_at": "2026-09-12T17:00:00Z",
                "status": "ok",
                "windows": [{"id": "weekly", "used_percent": 1}],
            }
        ],
        "attempts": [
            {
                "task": "flash-window-1",
                "provider": "opencode",
                "model": "glm-5.3-flash",
                "status": "completed",
                "at": "2026-09-12T16:20:00Z",
                "secret": "x",
            }
        ],
    }
    out = project(raw, overlay)
    assert [a["provider"] for a in out["accounts"]] == ["zai"]
    assert out["attempts"] == [
        {
            "task": "flash-window-1",
            "provider": "opencode",
            "model": "glm-5.3-flash",
            "status": "completed",
            "at": "2026-09-12T16:20:00Z",
        }
    ]
    assert project(raw, [])["attempts"][0]["task"] == "Unnamed claim"


def test_scorecard_overlay_is_sanitized_and_upstream_rows_are_ignored():
    rows = [
        {
            "family": "glm",
            "model": "glm-5.3-flash",
            "category": "pure_function",
            "attempts": 7,
            "completed": 6,
            "accepted": 6,
            "held": 0,
            "resolved": 1,
            "repairs": 0,
            "usage_reported": 7,
            "usage": {"input_tokens": 2641, "output_tokens": 10773},
            "task_names": ["secret task"],  # unknown key: stripped
            "prompt": "DO_NOT_SHARE",  # unknown key: stripped
        },
        {"family": "glm"},  # no model/category: dropped
        "junk",  # not a dict: dropped
    ]
    result = project({"accounts": [], "scorecard": [{"family": "upstream"}]}, {"scorecard": rows})
    assert result["scorecard"] == [
        {
            "family": "glm",
            "model": "glm-5.3-flash",
            "category": "pure_function",
            "attempts": 7,
            "completed": 6,
            "accepted": 6,
            "held": 0,
            "resolved": 1,
            "repairs": 0,
            "usage_reported": 7,
            "usage": {"input_tokens": 2641, "output_tokens": 10773},
        }
    ]


def test_scorecard_defaults_to_empty_without_an_overlay():
    result = project({"accounts": [], "scorecard": [{"family": "upstream"}]}, [])
    assert result["scorecard"] == []
    assert project({"accounts": []}, [])["scorecard"] == []
    assert project({"accounts": []}, {"attempts": []})["scorecard"] == []
