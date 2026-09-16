"""Read every plan's model catalogue and record it (brief 21 O1).

    python deployments/local/collect_catalogue.py <config.json> [--no-record]

One reading per plan, written beside the quota observations as `catalogue-<provider>.json`
and recorded into the ledger through `inference-grid catalogue-record`:

- Go: the documented model tables at https://opencode.ai/docs/go/ (prices, buckets, promos,
  endpoints, retention, request caps) — fetched with a plain GET, parsed by header, never
  by position.
- Command Code: `cmd --list-models` for the ids, sections and FREE tags, plus the rate the
  plan actually charged per model, summed from `~/.commandcode/projects/*/*.jsonl`
  (`usage.costUsd` per request) over the last 30 days.
- Z.ai: the window multiplier the quota reader already sees, recorded as a promo on the
  coding plan's models.

A plan whose reading fails writes a file with `status: error` and the exception name; the
others still record. Nothing here sends a prompt to any model.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".local/share/inference-grid-capacity/config.json"
USER_AGENT = "inference-grid-catalogue/1.0"


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def go_reading(catalogue):
    html = fetch(catalogue.GO_DOCS_URL)
    return catalogue.parse_go_table(html)


def command_code_reading(catalogue, config):
    cmd = config.get("goat_cmd_path", "/opt/homebrew/bin/cmd")
    env = dict(os.environ, PATH="/opt/homebrew/bin:" + os.environ.get("PATH", "/usr/bin:/bin"))
    proc = subprocess.run(
        [cmd, "--list-models"], capture_output=True, text=True, timeout=60, env=env
    )
    if proc.returncode != 0:
        raise RuntimeError("cmd --list-models rc " + str(proc.returncode))
    projects = Path(config.get("goat_projects_dir", Path.home() / ".commandcode/projects"))
    since = datetime.now(timezone.utc) - timedelta(days=30)
    return catalogue.command_code_rows(proc.stdout, projects, since)


def zai_reading(catalogue, config):
    """The coding plan's models with the window multiplier as a promo (the reader that
    knows the window is board_prepare; the fact it prints is re-read here from the
    quota file when present)."""
    rows = []
    window = None
    quota_path = config.get("zai_quota_path")
    if quota_path and Path(quota_path).is_file():
        try:
            window = json.loads(Path(quota_path).read_text()).get("window")
        except (OSError, ValueError):
            window = None
    for model in config.get("zai_models") or ["glm-5.3-flash"]:
        row = {
            "model": model,
            "display_name": model,
            "source": "config+quota-file",
            "residency": "unknown",
            "retention": "unknown",
        }
        if isinstance(window, dict) and window.get("multiplier") and window.get("active"):
            row["promo"] = f"window multiplier {window['multiplier']}x while active"
            row["promo_multiplier"] = float(window["multiplier"])
        rows.append(catalogue.normalise(row))
    return rows


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    record = "--no-record" not in args
    args = [a for a in args if a != "--no-record"]
    config_path = Path(args[0]) if args else DEFAULT_CONFIG
    config = json.loads(config_path.read_text())
    if config.get("package_src"):
        sys.path.insert(0, str(config["package_src"]))
    from inference_grid import catalogue

    out_dir = Path(config.get("output_dir", DEFAULT_CONFIG.parent))
    now = datetime.now(timezone.utc)
    readings = {
        "opencode": lambda: go_reading(catalogue),
        "command-code": lambda: command_code_reading(catalogue, config),
        "zai": lambda: zai_reading(catalogue, config),
    }
    summary = {}
    for provider, read in readings.items():
        document = {
            "provider": provider,
            "observed_at": now.isoformat(),
            "status": "ok",
            "rows": [],
        }
        try:
            document["rows"] = read()
        except Exception as exc:  # noqa: BLE001 — one plan's failure must not hide the others
            document["status"] = "error"
            document["error"] = type(exc).__name__ + ": " + str(exc)[:160]
        path = out_dir / f"catalogue-{provider}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(document, indent=1))
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        recorded = None
        if record and document["status"] == "ok" and document["rows"]:
            argv_record = [config.get("inference_grid_bin", "inference-grid")]
            if config.get("database_url"):
                argv_record += ["--database", config["database_url"]]
            # --json takes a file, not inline text.
            arg_path = out_dir / f"catalogue-{provider}.record.json"
            arg_path.write_text(json.dumps({"provider": provider, "path": str(path)}))
            argv_record += ["--json", str(arg_path), "catalogue-record"]
            proc = subprocess.run(argv_record, capture_output=True, text=True, timeout=120)
            recorded = proc.returncode == 0
            if not recorded:
                document["record_error"] = proc.stderr[-300:]
        summary[provider] = {
            "status": document["status"],
            "rows": len(document["rows"]),
            "recorded": recorded,
            "error": document.get("error") or document.get("record_error"),
        }
        print(provider, summary[provider])
    return 0 if all(v["status"] == "ok" for v in summary.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
