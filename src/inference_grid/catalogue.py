"""The plans' model catalogues: what each model costs on each plan, this week (brief 21 O1).

On 2026-09-16 the grid had run 121 reviews on Kimi K3 — the Go plan's most expensive model
($3/M in, $15/M out) with its smallest monthly bucket ($15) — while Qwen3.8 Flash, MiniMax
M3, a DeepSeek promo and a free model sat on the same credential. Nobody read the plan's
own table. This module reads it: the Go plan's documented model table, Command Code's
`cmd --list-models`, and the per-request rates Command Code's own session logs reveal.

Every reading becomes one `normalise()`d row per model — the same shape whatever the plan —
and the ledger keeps them as a series (`record_catalogue` / `catalogue`). Selection
(lanes/value.py) prices a task from these rows; `deals()` says what is cheap this week and
what it would take to use it.
"""

from __future__ import annotations

import html as _html
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

GO_DOCS_URL = "https://opencode.ai/docs/go/"
GO_PROVIDER = "opencode"
COMMAND_CODE_PROVIDER = "command-code"

# A "free" model's cost is a floor, never zero: value = quality / cost must stay finite,
# and a free model still leads every priced one.
FREE_COST_FLOOR_USD = 1e-6

ROW_KEYS = (
    "model",
    "display_name",
    "input_per_m",
    "output_per_m",
    "cache_read_per_m",
    "cache_write_per_m",
    "monthly_limit_usd",
    "promo",
    "promo_multiplier",
    "promo_ends",
    "free",
    "retention",
    "residency",
    "tier_note",
    "section",
    "source",
    "source_url",
    "parse_error",
)


def normalise(row: Dict[str, Any]) -> Dict[str, Any]:
    """One catalogue row in the shared shape; unknown fields refused, absent ones None.

    Prices are USD per million tokens as floats or None; `free` is a bool; `promo_ends`
    is an ISO date or None; `monthly_limit_usd` None means unlimited or unknown — the
    `promo`/`tier_note` text says which. A row is never invented: a parser that could not
    read a cell records None there and names the problem in `parse_error`.
    """
    if not isinstance(row, dict):
        raise ValueError("catalogue row must be a dict")
    unknown = sorted(set(row) - set(ROW_KEYS))
    if unknown:
        raise ValueError("catalogue row has unknown keys: " + ", ".join(unknown))
    model = row.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("catalogue row needs a model id")
    out: Dict[str, Any] = {k: row.get(k) for k in ROW_KEYS}
    for k in (
        "input_per_m",
        "output_per_m",
        "cache_read_per_m",
        "cache_write_per_m",
        "monthly_limit_usd",
        "promo_multiplier",
    ):
        v = out[k]
        if v is not None:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise ValueError(f"{k} must be a non-negative number or None")
            out[k] = float(v)
    out["free"] = bool(out["free"])
    if out["promo_ends"] is not None:
        try:
            date.fromisoformat(str(out["promo_ends"]))
        except ValueError:
            raise ValueError("promo_ends must be an ISO date") from None
        out["promo_ends"] = str(out["promo_ends"])
    for k in (
        "source",
        "source_url",
        "retention",
        "residency",
        "promo",
        "tier_note",
        "section",
        "display_name",
        "parse_error",
    ):
        if out[k] is not None and not isinstance(out[k], str):
            raise ValueError(f"{k} must be a str or None")
    if out["free"]:
        for k in ("input_per_m", "output_per_m", "cache_read_per_m"):
            if out[k] is None:
                out[k] = 0.0
    return out


# ---------------------------------------------------------------------------
# The Go plan: the documented model table
# ---------------------------------------------------------------------------
_MONEY = re.compile(r"\$\s*([0-9]+(?:\.[0-9]+)?)")
_MULT = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*[x×]", re.IGNORECASE)
_ENDS = re.compile(r"ends?\s+([A-Z][a-z]{2})\.?\s+([0-9]{1,2})", re.IGNORECASE)
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}


def _money(text: str) -> Optional[float]:
    m = _MONEY.search(text or "")
    return float(m.group(1)) if m else None


def _strip_tags(fragment: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", " ", fragment)).strip()


def _promo_ends(text: str, year: int) -> Optional[str]:
    m = _ENDS.search(text or "")
    if not m or m.group(1).lower() not in _MONTHS:
        return None
    try:
        return date(year, _MONTHS[m.group(1).lower()], int(m.group(2))).isoformat()
    except ValueError:
        return None


def _display_key(display: str) -> str:
    """'DeepSeek V4.1 Flash (Off-Peak)' and 'DeepSeek V4.1 Flash  4x · Ends Sep 20' -> the
    one display name the docs use across their tables: qualifiers and promo text dropped."""
    name = re.sub(r"\((?:off|peak|[<>≤≥]\s*\d+K?)[^)]*\)", "", display, flags=re.IGNORECASE)
    name = re.sub(r"\b\d+(?:\.\d+)?\s*[x×]\s*·?.*$", "", name, flags=re.IGNORECASE)
    # 'MiMo V2.5' in one table, 'MiMo-V2.5' in the next: punctuation is not identity.
    return re.sub(r"[\s_-]+", " ", name).strip().lower()


def go_model_id(display_name: str) -> str:
    """Fallback id when the docs' own id table lacks a row: 'Kimi K3' -> 'kimi-k3'."""
    return re.sub(r"[^a-z0-9.]+", "-", _display_key(display_name)).strip("-")


def _cells(tr: str) -> List[str]:
    return [
        _strip_tags(c)
        for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.IGNORECASE | re.DOTALL)
    ]


def _raw_cells(tr: str) -> List[str]:
    return re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.IGNORECASE | re.DOTALL)


def _table_kind(header: List[str]) -> Optional[str]:
    h = [x.lower() for x in header]
    if not h or "model" not in h[0]:
        return None
    joined = " ".join(h)
    if "input" in joined and "output" in joined:
        return "prices"
    if "model id" in joined or "endpoint" in joined:
        return "ids"
    if "retention" in joined:
        return "retention"
    if "requests per" in joined:
        return "requests"
    return None


def parse_go_table(html_text: str, year: Optional[int] = None) -> List[Dict[str, Any]]:
    """Rows from the Go docs' model tables (HTML), joined by display name.

    The page carries four tables that matter, read by their headers so order does not:
    prices (input/output/cached per M, monthly limit — a promo shows as
    `<del>$15</del> <strong>$60</strong> 4x · Ends Sep 20` in the limit cell, a free
    model as "Free"/"Unlimited"), model ids with each model's ENDPOINT (chat/completions,
    messages, responses — a protocol fact the lane must honour), retention, and request
    caps per window. Peak/off-peak or context-size pairs are one model with two prices:
    the first (off-peak / small-context) row is kept and the other noted. A cell that
    cannot be read leaves None and a `parse_error`; the row is never dropped."""
    year = year or date.today().year
    prices: Dict[str, Dict[str, Any]] = {}
    ids: Dict[str, Dict[str, Any]] = {}
    retention: Dict[str, Dict[str, Any]] = {}
    requests: Dict[str, Dict[str, Any]] = {}
    for table in re.findall(r"<table.*?</table>", html_text, flags=re.IGNORECASE | re.DOTALL):
        header = [
            _strip_tags(c)
            for c in re.findall(r"<th[^>]*>(.*?)</th>", table, flags=re.IGNORECASE | re.DOTALL)
        ]
        kind = _table_kind(header)
        if kind is None:
            continue
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table, flags=re.IGNORECASE | re.DOTALL):
            cells, raw = _cells(tr), _raw_cells(tr)
            if not cells or not cells[0]:
                continue
            key = _display_key(cells[0])
            if kind == "prices":
                row: Dict[str, Any] = {"display_name": cells[0], "errors": []}
                everything = " ".join(cells[1:])
                free = "free" in everything.lower() and _money(everything) is None
                row["free"] = free
                for name, idx in (
                    ("input_per_m", 1),
                    ("output_per_m", 2),
                    ("cache_read_per_m", 3),
                    ("cache_write_per_m", 4),
                ):
                    if idx >= len(cells):
                        row["errors"].append(f"no {name} cell")
                        continue
                    v = _money(cells[idx])
                    if v is None and not free and cells[idx].strip() not in ("-", "—", ""):
                        row["errors"].append(f"{name}: {cells[idx]!r}")
                    row[name] = v
                limit_raw = raw[5] if len(raw) > 5 else ""
                limit_text = cells[5] if len(cells) > 5 else ""
                strong = re.search(r"<strong>(.*?)</strong>", limit_raw, flags=re.DOTALL)
                base = re.search(r"<del>(.*?)</del>", limit_raw, flags=re.DOTALL)
                row["monthly_limit_usd"] = (
                    _money(_strip_tags(strong.group(1))) if strong else _money(limit_text)
                )
                if (
                    row["monthly_limit_usd"] is None
                    and "unlimited" not in limit_text.lower()
                    and limit_text
                ):
                    row["errors"].append(f"monthly_limit: {limit_text!r}")
                mult = _MULT.search(limit_text)
                if mult:
                    row["promo_multiplier"] = float(mult.group(1))
                    base_limit = _money(_strip_tags(base.group(1))) if base else None
                    base_text = f" (base ${base_limit:.0f}/month)" if base_limit is not None else ""
                    small = re.search(r"<small>(.*?)</small>", limit_raw, flags=re.DOTALL)
                    row["promo"] = (
                        _strip_tags(small.group(1)) if small else limit_text
                    ).strip() + base_text
                    row["promo_ends"] = _promo_ends(limit_text, year)
                elif "limited time" in limit_text.lower():
                    row["promo"] = "limited time"
                if re.search(r"\((?:peak|>\s*\d)", cells[0], re.IGNORECASE):
                    row["is_secondary"] = True
                if key in prices and not row.get("is_secondary"):
                    prices[key]["errors"].append("duplicate primary price row")
                if key not in prices or (
                    prices[key].get("is_secondary") and not row.get("is_secondary")
                ):
                    row["secondary"] = prices.get(key, {}).get("display_name")
                    prices[key] = row
                else:
                    prices[key]["secondary"] = (
                        f"{cells[0]}: in ${row.get('input_per_m')}/M out ${row.get('output_per_m')}/M"
                    )
            elif kind == "ids" and len(cells) >= 3:
                ids[key] = {
                    "model": cells[1].strip(),
                    "endpoint": cells[2].strip(),
                    "protocol": cells[2].rsplit("/v1/", 1)[-1].strip("/")
                    if "/v1/" in cells[2]
                    else None,
                }
            elif kind == "retention" and len(cells) >= 3:
                ret = cells[2].lower()
                retention[key] = {
                    "retention": (
                        "zero"
                        if "0 day" in ret
                        else "not-zdr"
                        if "not zdr" in ret
                        else "days"
                        if "day" in ret
                        else None
                    ),
                    "training": cells[1].strip(),
                }
            elif kind == "requests" and len(cells) >= 4:
                requests[key] = {"per_5h": cells[1], "per_week": cells[2], "per_month": cells[3]}
    rows: List[Dict[str, Any]] = []
    for key in sorted(set(prices) | set(ids)):
        p = prices.get(key, {"display_name": key, "errors": ["no price row"], "free": False})
        i = ids.get(key, {})
        model = i.get("model") or go_model_id(p["display_name"])
        notes = []
        if i.get("protocol"):
            notes.append(f"endpoint {i['protocol']}")
        if p.get("secondary"):
            notes.append(str(p["secondary"]))
        if requests.get(key):
            r = requests[key]
            notes.append(f"requests {r['per_5h']}/5h {r['per_week']}/wk {r['per_month']}/mo")
        if retention.get(key, {}).get("training"):
            notes.append("training: " + retention[key]["training"])
        rows.append(
            normalise(
                {
                    "model": model,
                    "display_name": p["display_name"],
                    "free": bool(p.get("free")),
                    "input_per_m": p.get("input_per_m"),
                    "output_per_m": p.get("output_per_m"),
                    "cache_read_per_m": p.get("cache_read_per_m"),
                    "cache_write_per_m": p.get("cache_write_per_m"),
                    "monthly_limit_usd": p.get("monthly_limit_usd"),
                    "promo": p.get("promo"),
                    "promo_multiplier": p.get("promo_multiplier"),
                    "promo_ends": p.get("promo_ends"),
                    "retention": retention.get(key, {}).get("retention"),
                    "residency": "us",
                    "tier_note": "; ".join(notes) or None,
                    "section": i.get("protocol"),
                    "source": "docs-table",
                    "source_url": GO_DOCS_URL,
                    "parse_error": "; ".join(p.get("errors") or []) or None,
                }
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Command Code: `cmd --list-models` plus the observed rate per model
# ---------------------------------------------------------------------------
_CMD_LINE = re.compile(r"^(?P<id>[a-z0-9][a-z0-9._/:-]*)\s{2,}(?P<blurb>.+)$", re.IGNORECASE)


def parse_cmd_models(text: str) -> List[Dict[str, Any]]:
    """Rows from `cmd --list-models`: section headings (a line with no two-space gap and
    no slash) label the rows below them; FREE is read from the id's `:free`/`-free` tag or
    a blurb starting with FREE. No prices: Command Code publishes none the CLI can reach."""
    rows: List[Dict[str, Any]] = []
    section = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lower().startswith("available models"):
            continue
        m = _CMD_LINE.match(line.strip())
        if m and ("/" in m.group("id") or "-" in m.group("id")):
            mid, blurb = m.group("id"), m.group("blurb").strip()
            free = bool(
                re.search(r"(:free|-free)$", mid, re.IGNORECASE)
            ) or blurb.upper().startswith("FREE")
            rows.append(
                normalise(
                    {
                        "model": mid,
                        "display_name": mid,
                        "free": free,
                        "section": section,
                        "tier_note": blurb,
                        "source": "cmd-list-models",
                    }
                )
            )
        elif not m and not line.startswith(" "):
            section = line.strip()
    return rows


def observed_rates(
    projects_dir: Path, since: Optional[datetime] = None
) -> Dict[str, Dict[str, Any]]:
    """Per-model rates from Command Code's own session logs: every request row there
    carries `usage` with token counts and `costUsd`, so the plan's price per model is
    what it charged, summed. Returns {model: {input_per_m, output_per_m, requests,
    cost_usd, tokens}} — one blended rate per model (cache reads folded into input,
    because the logs price the request as a whole)."""
    totals: Dict[str, Dict[str, float]] = {}
    for path in Path(projects_dir).glob("*/*.jsonl"):
        if path.name.endswith(".checkpoints.jsonl"):
            continue
        try:
            if (
                since is not None
                and datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) < since
            ):
                continue
            with path.open("rb") as handle:
                for raw in handle:
                    if b'"usage"' not in raw:
                        continue
                    try:
                        row = json.loads(raw)
                    except ValueError:
                        continue
                    usage = _find_usage(row)
                    model = _find_model(row)
                    if not usage or not model:
                        continue
                    t = totals.setdefault(
                        model, {"input": 0.0, "output": 0.0, "cost_usd": 0.0, "requests": 0.0}
                    )
                    t["input"] += float(usage.get("inputTokens") or 0) + float(
                        usage.get("cacheReadTokens") or 0
                    )
                    t["output"] += float(usage.get("outputTokens") or 0)
                    t["cost_usd"] += float(usage.get("costUsd") or 0)
                    t["requests"] += 1
        except OSError:
            continue
    out: Dict[str, Dict[str, Any]] = {}
    for model, t in totals.items():
        tokens = t["input"] + t["output"]
        blended = (t["cost_usd"] / tokens * 1e6) if tokens else None
        out[model] = {
            "input_per_m": blended,
            "output_per_m": blended,
            "requests": int(t["requests"]),
            "cost_usd": round(t["cost_usd"], 6),
            "tokens": int(tokens),
        }
    return out


def _find_usage(row: Any) -> Optional[Dict[str, Any]]:
    if isinstance(row, dict):
        u = row.get("usage")
        if isinstance(u, dict) and "costUsd" in u:
            return u
        for v in row.values():
            found = _find_usage(v)
            if found:
                return found
    elif isinstance(row, list):
        for v in row:
            found = _find_usage(v)
            if found:
                return found
    return None


def _find_model(row: Any) -> Optional[str]:
    if isinstance(row, dict):
        for k in ("model", "modelId", "model_id"):
            v = row.get(k)
            if isinstance(v, str) and v:
                return v
        for v in row.values():
            found = _find_model(v)
            if found:
                return found
    elif isinstance(row, list):
        for v in row:
            found = _find_model(v)
            if found:
                return found
    return None


def command_code_rows(
    list_models_text: str, projects_dir: Optional[Path], since: Optional[datetime] = None
) -> List[Dict[str, Any]]:
    """`cmd --list-models` rows with the observed rate filled in where the logs have one."""
    rows = parse_cmd_models(list_models_text)
    rates = (
        observed_rates(projects_dir, since) if projects_dir and Path(projects_dir).is_dir() else {}
    )
    out = []
    for r in rows:
        rate = rates.get(r["model"])
        if rate and not r["free"]:
            r = dict(
                r,
                input_per_m=rate["input_per_m"],
                output_per_m=rate["output_per_m"],
                source="cmd-list-models+observed",
                tier_note=(r.get("tier_note") or "")
                + f" [observed {rate['requests']} requests, ${rate['cost_usd']:.4f}]",
            )
        out.append(normalise(r))
    return out


# ---------------------------------------------------------------------------
# Pricing a task and reporting the deals
# ---------------------------------------------------------------------------
def cost_estimate(
    row: Optional[Dict[str, Any]],
    input_tokens: float,
    output_tokens: float,
    cache_tokens: float = 0.0,
    at: Optional[date] = None,
) -> Optional[float]:
    """Dollars for a call of this size on this catalogue row; None without a price.

    A promo multiplier divides the price while `promo_ends` is today or later (a 4x
    promo makes the bucket go four times as far, which is the same as a quarter of the
    price); a free row costs the floor. Cache reads use the cache price when the row has
    one, else the input price."""
    if not row:
        return None
    if row.get("free"):
        return FREE_COST_FLOOR_USD
    pi, po = row.get("input_per_m"), row.get("output_per_m")
    if pi is None or po is None:
        return None
    pc = row.get("cache_read_per_m")
    pc = pi if pc is None else pc
    usd = (pi * max(input_tokens, 0) + po * max(output_tokens, 0) + pc * max(cache_tokens, 0)) / 1e6
    mult = row.get("promo_multiplier")
    ends = row.get("promo_ends")
    at = at or date.today()
    if mult and mult > 0 and (ends is None or date.fromisoformat(ends) >= at):
        usd /= mult
    return max(usd, FREE_COST_FLOOR_USD)


def promo_active(row: Dict[str, Any], at: Optional[date] = None) -> bool:
    at = at or date.today()
    if row.get("free"):
        return True
    if row.get("promo_multiplier"):
        ends = row.get("promo_ends")
        return ends is None or date.fromisoformat(ends) >= at
    return bool(row.get("promo"))


def deals(
    catalogue: Iterable[Dict[str, Any]],
    lanes: Dict[str, Dict[str, Any]],
    readiness: Optional[Dict[str, Dict[str, Any]]] = None,
    at: Optional[date] = None,
    ending_within_days: int = 3,
) -> List[Dict[str, Any]]:
    """What is cheap this week and what it would take to use it.

    One line per free or promo model: the plan, the deal, and its state — `not a lane`,
    `lane unqualified` (a lane exists but readiness says it has no canary), `qualified` —
    plus a `ending_soon` flag when the promo ends within `ending_within_days`."""
    at = at or date.today()
    readiness = readiness or {}
    by_model: Dict[str, List[str]] = {}
    for lane_id, lane in lanes.items():
        m = str(lane.get("model") or "")
        for key in {m, m.rsplit("/", 1)[-1]}:
            by_model.setdefault(key, []).append(lane_id)
    out = []
    for row in catalogue:
        if not promo_active(row, at):
            continue
        lane_ids = sorted(
            set(by_model.get(row["model"], []) + by_model.get(row["model"].rsplit("/", 1)[-1], []))
        )
        if not lane_ids:
            state = "not a lane"
        else:
            states = [readiness.get(lid, {}).get("state") for lid in lane_ids]
            state = (
                "qualified"
                if any(s == "ready" for s in states)
                else (
                    "lane unqualified: canary pending"
                    if any(s == "unqualified" for s in states)
                    else "lane " + "/".join(str(s) for s in states)
                )
            )
        ends = row.get("promo_ends")
        days_left = (date.fromisoformat(ends) - at).days if ends else None
        if row.get("free"):
            deal = "free"
        elif row.get("promo"):
            deal = row["promo"]
        else:
            deal = f"{row.get('promo_multiplier'):g}x promo"
        out.append(
            {
                "provider": row.get("provider"),
                "model": row["model"],
                "deal": deal,
                "promo_ends": ends,
                "days_left": days_left,
                "ending_soon": days_left is not None and days_left <= ending_within_days,
                "lanes": lane_ids,
                "state": state,
                "monthly_limit_usd": row.get("monthly_limit_usd"),
            }
        )
    out.sort(key=lambda d: (d["days_left"] if d["days_left"] is not None else 10**6, d["model"]))
    return out


def deals_lines(rows: List[Dict[str, Any]]) -> List[str]:
    lines = []
    for d in rows:
        when = (
            f", ends in {d['days_left']}d ({d['promo_ends']})" if d["days_left"] is not None else ""
        )
        bucket = (
            "unlimited"
            if d["monthly_limit_usd"] is None
            else f"${d['monthly_limit_usd']:.0f}/month"
        )
        lines.append(f"{d['provider']}/{d['model']}: {d['deal']}{when}; {bucket} — {d['state']}")
    return lines
