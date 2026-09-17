from inference_grid.capacity import (
    clean_disk_usage,
    clean_failures,
    clean_heartbeats,
    clean_operator,
    project,
)


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


def test_capacity_panel_overlay_is_sanitized():
    rows = [
        {
            "provider": "zai",
            "landed_count": 3,
            "landed_consumed": 5.5,
            "failed_abandoned_count": 1,
            "failed_abandoned_consumed": 2.0,
            "secret": "DO_NOT_SHARE",  # unknown key: stripped
        },
        {"provider": "not-a-real-provider", "landed_count": 1},  # unrecognized: dropped
        "junk",  # not a dict: dropped
    ]
    result = project({"accounts": []}, {"capacity_panel": rows})
    assert result["capacity_panel"] == [
        {
            "provider": "zai",
            "landed_count": 3,
            "landed_consumed": 5.5,
            "failed_abandoned_count": 1,
            "failed_abandoned_consumed": 2.0,
        }
    ]
    assert "secret" not in str(result["capacity_panel"])


def test_capacity_panel_bad_numbers_become_zero_not_dropped():
    rows = [
        {
            "provider": "zai",
            "landed_count": "not a number",
            "landed_consumed": float("nan"),
            "failed_abandoned_count": -1,
            "failed_abandoned_consumed": -5.0,
        }
    ]
    result = project({"accounts": []}, {"capacity_panel": rows})
    assert result["capacity_panel"] == [
        {
            "provider": "zai",
            "landed_count": 0,
            "landed_consumed": 0.0,
            "failed_abandoned_count": 0,
            "failed_abandoned_consumed": 0.0,
        }
    ]


def test_capacity_panel_defaults_to_empty_without_an_overlay():
    assert project({"accounts": []}, [])["capacity_panel"] == []
    assert project({"accounts": []}, {"attempts": []})["scorecard"] == []


def test_capacity_panel_unknown_provider_is_dropped_not_displayed():
    """overlay_build.capacity_panel() emits an "unknown" row for an unmapped lane
    (see CapacityPanelTests in tests/test_local_collectors.py) -- it must not reach
    the dashboard just because it's the only row for that lane."""
    rows = [
        {
            "provider": "unknown",
            "landed_count": 3,
            "landed_consumed": 5.0,
            "failed_abandoned_count": 0,
            "failed_abandoned_consumed": 0.0,
        }
    ]
    assert project({"accounts": []}, {"capacity_panel": rows})["capacity_panel"] == []


# --- 02-A1 open half: heartbeats, disk usage, failures ---


def test_clean_heartbeats_keeps_named_rows_with_unknown_age_as_none():
    rows = [
        {
            "name": "tick-boards",
            "age_seconds": 42.5,
            "since": "2026-09-17T00:00:00+00:00",
            "junk": 1,
        },
        {"name": "zai", "age_seconds": None, "since": ""},
        {"name": "", "age_seconds": 10, "since": ""},  # dropped: no name
        "not a dict",
    ]
    result = clean_heartbeats(rows)
    assert result == [
        {"name": "tick-boards", "age_seconds": 42.5, "since": "2026-09-17T00:00:00+00:00"},
        {"name": "zai", "age_seconds": None, "since": ""},
    ]


def test_clean_heartbeats_defaults_to_empty_without_an_overlay():
    assert project({"accounts": []}, [])["heartbeats"] == []


def test_clean_disk_usage_keeps_the_two_counts_never_the_path():
    usage = {
        "root": "/Users/james/.grid-workspaces",
        "exists": True,
        "total_bytes": 100,
        "cap_bytes": 4000000000,
        "over_cap": False,
    }
    result = clean_disk_usage(usage)
    assert result == {
        "exists": True,
        "total_bytes": 100.0,
        "cap_bytes": 4000000000.0,
        "over_cap": False,
    }
    assert "root" not in result


def test_clean_disk_usage_rejects_bad_shapes():
    assert clean_disk_usage(None) is None
    assert clean_disk_usage({"total_bytes": "nope", "cap_bytes": 10}) is None
    assert clean_disk_usage({"total_bytes": 10, "cap_bytes": 0}) is None


def test_clean_disk_usage_defaults_to_none_without_an_overlay():
    assert project({"accounts": []}, [])["disk_usage"] is None


def test_clean_failures_bounds_the_log_tail_and_drops_unknown_kinds():
    rows = [
        {
            "kind": "held_attempt",
            "id": "a1",
            "task": "t1",
            "reason": "rounds_exhausted",
            "since": "2026-09-17T00:00:00+00:00",
            "log_tail": "x" * 5000,
            "suggestion": "raise max_rounds",
            "credential": "secret",
        },
        {"kind": "queued", "id": "a2"},  # dropped: not a failure kind
        {"kind": "failed_attempt", "id": ""},  # dropped: no id
    ]
    result = clean_failures(rows)
    assert len(result) == 1
    assert result[0]["kind"] == "held_attempt"
    assert result[0]["id"] == "a1"
    assert len(result[0]["log_tail"]) == 4096
    assert "credential" not in result[0]
    assert "secret" not in str(result)


def test_clean_failures_defaults_to_empty_without_an_overlay():
    assert project({"accounts": []}, [])["failures"] == []


def test_clean_operator_keeps_the_newest_50_rather_than_truncating_by_source_order():
    # 60 dated held_attempt rows, oldest-declared first, plus one undated alarm.
    # Sorting purely by (kind, id) before truncating -- the bug found in review --
    # would keep the 50 alphabetically-first rows (and always keep "alarm", since it
    # sorts before "held_attempt"); the fix must instead keep the 50 *newest by since*,
    # with the undated alarm surviving too since a missing since must read as "keep".
    dated = [
        {"kind": "held_attempt", "id": f"a{i:02d}", "since": f"2026-09-17T00:{i:02d}:00+00:00"}
        for i in range(60)
    ]
    undated_alarm = {"kind": "alarm", "id": "zzz-no-since"}
    result = clean_operator(dated + [undated_alarm])
    assert len(result) == 50
    kept_ids = {r["id"] for r in result}
    assert "a59" in kept_ids  # the latest-timestamped row
    assert "a00" not in kept_ids  # the earliest-timestamped row is evicted
    assert "zzz-no-since" in kept_ids  # undated is never the first thing dropped
