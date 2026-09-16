# Authored by ZCode (GLM-5.3-Flash) through Grid attempt zcode-go-lane-1; coordinator edits:
# package imports for structured_output and the receipts safe_path (see docs/CONTRIBUTIONS.md).
"""OpenCode Go lane: one chat-completions call to the subscription endpoint (kind go_http).

Evidence is the endpoint's own JSON document, preserved in native.json, with usage and finish
reason recorded in the verdict, so qualification never trusts prose. The API key is read from a
0o600 credential file and lives only in the request headers and the injected send contract; it
is never written into the verdict, receipt or any file under the attempt directory. No
subprocess runs here, so no sandbox profile is needed.
"""

import ast
import json
import shutil
import stat
import urllib.error
import urllib.request
from pathlib import Path

from .. import structured_output
from ..receipts import safe_path

# The chat-completions endpoint is a property of the lane's provider (brief 15, G1):
# lanes.json is provider-authored, so the mapping lives here and an unknown provider
# refuses before credentials are read or anything is sent. ClinePass exposes the same
# OpenAI-compatible protocol with the response wrapped as {"data": {...}} and a
# narrower subscription plan (docs/LANES.md, `cline-http`).
ENDPOINTS = {
    "opencode": "https://opencode.ai/zen/go/v1/chat/completions",
    "clinepass": "https://api.cline.bot/api/v1/chat/completions",
}
ENDPOINT = ENDPOINTS["opencode"]
DEFAULT_PROVIDER = "opencode"
USER_AGENT = "inference-grid-lane/1.0"
MAX_BODY = 4 * 1024 * 1024
MAX_CONTENT = 40000
# Generous room for the reply itself when a thinking budget caps max_tokens; the reply
# is a single JSON object.
CONTENT_ALLOWANCE = 4000
# max_tokens is a spend guard, not a thinking limit: the endpoint shapes reasoning only by
# the categorical reasoning_effort below, never by a token count, and on 2026-09-14 kimi-k3
# at "high" reasoned 1.4-1.7x a 6 000 budget on 16-18 K-token review prompts — the one
# capped at thinking_tokens + CONTENT_ALLOWANCE (10 000) overran with no content, mid-way
# through a real finding. Three times the budget leaves that room and still bounds a runaway.
REASONING_HEADROOM = 3
# Capability map: the reasoning_effort values the model's documented request schema
# honours on this OpenAI-compatible endpoint. kimi-k3: low/high/max, endpoint default
# max (OpenCode Zen docs). deepseek-v4-flash: DeepSeek's own ChatCompletions schema
# documents a thinking toggle plus reasoning_effort none/low/high/max, default high —
# "none" would disable thinking, which review tasks never want; whether the Go gateway
# passes the field through for the model is unverified until the first canary, and the
# verdict plus unsupported_until will say.
REASONING_EFFORT = {
    "kimi-k3": ("low", "high", "max"),
    "deepseek-v4-flash": ("low", "high", "max"),
}
# The coordinator's policy mapping a task's thinking_tokens to an effort tier
# (docs/BOARD.md): absent or at most 4000 → low, up to 12000 → high, beyond → max.
EFFORT_TIERS = ((4000, "low"), (12000, "high"))


def reasoning_effort_for(thinking_tokens):
    """The effort tier for a task's thinking budget under EFFORT_TIERS, max beyond.

    An absent budget tiers as low: the mapped models reason by default, so an unbudgeted
    task still asks for the cheapest effort the model accepts.
    """
    if thinking_tokens is None:
        return "low"
    for cap, effort in EFFORT_TIERS:
        if thinking_tokens <= cap:
            return effort
    return "max"


class RefusedRedirects(urllib.request.HTTPRedirectHandler):
    """Turn every redirect answer into a transport error instead of a silent hop."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirect refused")


class ResponseTooLarge(Exception):
    """The native response body exceeded the read bound."""


def stage_inputs(request, work):
    names = []
    root = Path(request["input_directory"])
    for path in sorted(root.rglob("*")):
        if path.is_file():
            relative = path.relative_to(root)
            target = work / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            names.append(str(relative))
    return names


def read_key(credential_path):
    """Return (api_key, None), or (None, refusal reason) for an unusable credential file."""
    path = Path(credential_path)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None, "credential file missing"
    if mode != 0o600:
        return None, "credential file must be mode 0o600"
    try:
        document = json.loads(path.read_text())
    except ValueError:
        return None, "credential file is not valid JSON"
    # Two shapes: OpenCode's own auth.json ({"opencode-go": {"key": ...}}) for the Go
    # subscription, and the grid's credential file ({"api_key": ...}) for any other
    # provider the lane kind is pointed at (ClinePass).
    key = None
    if isinstance(document, dict):
        entry = document.get("opencode-go")
        key = entry.get("key") if isinstance(entry, dict) else document.get("api_key")
    if not isinstance(key, str) or not key:
        return None, "credential key is empty (expected opencode-go.key or api_key)"
    return key, None


def endpoint_for(provider):
    """The chat-completions URL for a lane provider; ValueError names an unknown one."""
    try:
        return ENDPOINTS[provider]
    except (KeyError, TypeError):
        raise ValueError("unknown provider: " + str(provider)) from None


# The model id the endpoint bills the subscription under (brief 14, L6): ClinePass
# draws on the pass only for ids in its own `cline-pass/` namespace — a vendor id
# (`z-ai/glm-5.3-flash`) is served, when at all, from a free tier whose daily cap the
# first canary exhausted (`endpoint returned HTTP 429`, grid/board/canary-cline-http.json,
# 2026-09-16), and an uncovered vendor id 402s (model_not_in_plan). The lane record
# keeps the canonical id — the runner's model match and the scorecard key on it — and
# the wire carries `cline-pass/<bare id>`.
PASS_NAMESPACES = {"clinepass": "cline-pass/"}


def wire_model(provider, model):
    """The model id to put on the wire for a provider; the lane's own id otherwise."""
    namespace = PASS_NAMESPACES.get(provider)
    if namespace is None or not isinstance(model, str):
        return model
    return namespace + model.rsplit("/", 1)[-1]


def expected_artifact(work):
    """The single artifact name listed in inputs/expected.json."""
    try:
        names = json.loads((work / "expected.json").read_text())
    except (OSError, ValueError):
        return None, "expected.json must list exactly one artifact name"
    if not isinstance(names, list) or len(names) != 1:
        return None, "expected.json must list exactly one artifact name"
    if not isinstance(names[0], str) or not safe_path(names[0]):
        return None, "expected.json must list safe relative artifact names"
    return names[0], None


def http_send(body, key, session, timeout, endpoint=ENDPOINT):
    """POST one chat completion to endpoint; return the parsed JSON document."""
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "x-opencode-session": session,
        },
        method="POST",
    )
    opener = urllib.request.build_opener(RefusedRedirects())
    with opener.open(request, timeout=timeout) as stream:
        raw = stream.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        raise ResponseTooLarge("native response exceeds 4 MiB")
    return json.loads(raw)


def qualify(response, model, aliases=()):
    """The reply must come from the asked model, have stopped normally, and carry text.

    `aliases` are the other ids the one asked model legitimately answers under: a
    ClinePass request carries the pass id (wire_model) while the endpoint may echo the
    lane's configured vendor id — one model behind two ids, the runner's bare_model
    rule. Any other id is still an unexpected model.
    """
    if not isinstance(response, dict):
        return "native response was not a JSON object"
    served = response.get("model")
    if served != model and served not in aliases:
        return "request served by an unexpected model"
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return "native response carried no choices"
    if choices[0].get("finish_reason") != "stop":
        return "final native request did not stop normally"
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        return "empty terminal text"
    return None


def region_optin_refusal(error):
    """`region_optin_required` when a 403 body says the hosting opt-in is off; else None.

    The body is read only to classify, bounded and never recorded: the operator should
    see "enable China hosting in the Go console", not a bare 403 or the error page.
    """
    try:
        body = error.read(65536)
    except Exception:
        return None
    text = body.decode("utf-8", "ignore").lower()
    if any(marker in text for marker in ("region", "opt-in", "opt_in", "china")):
        return "region_optin_required: enable China hosting in the Go console"
    return None


def unwrap_data(response):
    """(chat-completions document, provider) — ClinePass wraps the document as {"data": …}.

    The wrapper carries the provider field the verdict records; when it does not, the
    inner document's own provider field serves. A response that is not wrapped passes
    through unchanged with whatever provider field it carries (None for the Go endpoint).
    """
    wrapped = {}
    if (
        isinstance(response, dict)
        and "choices" not in response
        and isinstance(response.get("data"), dict)
    ):
        wrapped, document = response, response["data"]
    else:
        document = response
    provider = wrapped.get("provider")
    if provider is None and isinstance(document, dict):
        provider = document.get("provider")
    return document, provider


def _content_of(response):
    """The reply text of the first choice, or None."""
    choices = response.get("choices") if isinstance(response, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    return message.get("content") if isinstance(message, dict) else None


def tool_markup_refusal(content):
    """`tool_markup` when the reply is a tool-calling transcript instead of the verdict.

    review-ff-glm-r6-c8e5c2c came back as <|open|>tools<|sep|><|open|>call tool="bash"…;
    the schema test failed it correctly but only as failed_tests. The refusal carries the
    first 120 characters so the hold reason shows what came back.
    """
    if not isinstance(content, str):
        return None
    if content.lstrip().startswith("<|") or "call tool=" in content:
        return (
            "tool_markup: the reply is a tool-calling transcript, not a verdict: "
            + content.strip()[:120]
        )
    return None


def reasoning_overrun(response, max_tokens):
    """The refusal for a length stop with no content: the model thought its whole output away.

    Carries the usage reasoning count and the cap so the hold reason and board-status name
    the overrun instead of a generic did-not-stop. None when the reply produced text
    (plain truncation stays generic) or stopped any other way.
    """
    choices = response.get("choices") if isinstance(response, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    if choices[0].get("finish_reason") != "length":
        return None
    content = _content_of(response)
    if isinstance(content, str) and content.strip():
        return None
    usage = response.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    reasoning = usage.get("reasoning_tokens")
    details = usage.get("completion_tokens_details")
    if reasoning is None and isinstance(details, dict):
        reasoning = details.get("reasoning_tokens")
    if type(reasoning) is not int:
        reasoning = usage.get("completion_tokens")
    return (
        "reasoning_overrun: finish_reason length with no content"
        + f" (reasoning_tokens {reasoning} of max_tokens {max_tokens})"
    )


def expected_code(content):
    """The python source of the {"code": ...} object the brief demands, or a refusal reason."""
    try:
        document = structured_output.parse_json_object(content, max_chars=MAX_CONTENT)
    except ValueError:
        return None, "invalid expected code schema"
    code = document.get("code") if set(document) == {"code"} else None
    if not isinstance(code, str) or not code:
        return None, "invalid expected code schema"
    try:
        ast.parse(code)
    except (SyntaxError, ValueError):
        return None, "artifact is not valid python"
    return code, None


def run(request, lane, attempt_dir, *, send=None, max_tokens=16000, timeout=None):
    """Execute one attempt; return (receipt or None, verdict). The caller prints the receipt."""
    attempt_dir = Path(attempt_dir)
    work = attempt_dir / "work"
    work.mkdir(mode=0o700)
    staged = stage_inputs(request, work)
    prompt = (work / "brief.txt").read_text().strip()
    # One request, no tools: every other staged text input travels in the prompt itself.
    for name in staged:
        if name in ("brief.txt", "expected.json"):
            continue
        try:
            content = (work / name).read_text()
        except (UnicodeDecodeError, OSError):
            continue
        prompt += "\n\n=== " + name + " ===\n" + content
    session = request["attempt"]
    verdict = {
        "supervisor": None,
        "session": session,
        "provider": None,
        "usage": None,
        "finish_reason": None,
        "refusal": None,
    }
    # The endpoint is a property of the lane's provider (docs/LANES.md): an unknown one
    # refuses before credentials are read or a request is built.
    provider = lane.get("provider", DEFAULT_PROVIDER)
    try:
        endpoint = endpoint_for(provider)
    except ValueError as exc:
        verdict["refusal"] = str(exc)
        return None, verdict
    key, problem = read_key(lane["credential_path"])
    if problem:
        verdict["refusal"] = problem
        return None, verdict
    # The transport waits for the tighter of the task budget and the lane budget, bounded:
    # 16k-token reviews outran a fixed bound, and a lane must never wait longer than its
    # record allows. An explicit timeout (tests) always wins. Both bounds go to the verdict.
    task_wall = request.get("wall_seconds")
    lane_wall = lane["wall_seconds"]
    bounds = [lane_wall, 600]
    if type(task_wall) is int and task_wall > 0:
        bounds.append(task_wall)
    transport_timeout = timeout if timeout is not None else min(bounds)
    verdict["transport_timeout"] = transport_timeout
    verdict["task_wall_seconds"] = task_wall
    verdict["lane_wall_seconds"] = lane_wall
    thinking_tokens = request.get("thinking_tokens")
    if type(thinking_tokens) is int and thinking_tokens > 0:
        max_tokens = REASONING_HEADROOM * thinking_tokens + CONTENT_ALLOWANCE
        verdict["thinking_tokens"] = thinking_tokens
    verdict["max_tokens"] = max_tokens
    # The wire carries the id the provider bills the subscription under (wire_model);
    # the lane's own id stays the receipt's actual_model, and the verdict records the
    # wire id whenever it differs so a hold names what was actually asked for.
    sent_model = wire_model(provider, request["model"])
    if sent_model != request["model"]:
        verdict["wire_model"] = sent_model
    body = {
        "model": sent_model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "max_tokens": max_tokens,
    }
    # What the endpoint honours is categorical reasoning_effort, per model
    # (REASONING_EFFORT, tiers via reasoning_effort_for/EFFORT_TIERS, policy in
    # docs/BOARD.md). The token count itself travels only as the max_tokens cap above,
    # so a model outside the map says reasoning_budget: unsupported.
    effort = reasoning_effort_for(thinking_tokens if type(thinking_tokens) is int else None)
    if effort in REASONING_EFFORT.get(request.get("model"), ()):
        body["reasoning_effort"] = effort
        verdict["reasoning_effort"] = effort
    else:
        verdict["reasoning_effort"] = "unsupported"
        if verdict.get("thinking_tokens") is not None:
            verdict["reasoning_budget"] = "unsupported"
    try:
        if send is not None:
            response = send(body, key, session, transport_timeout)
        else:
            response = http_send(body, key, session, transport_timeout, endpoint)
    except urllib.error.HTTPError as exc:
        # The status code and a fixed reason only; the error body is read solely to
        # classify a hosting opt-in 403 and is never recorded.
        verdict["refusal"] = f"endpoint returned HTTP {exc.code}"
        if exc.code == 402:
            # ClinePass bills models outside the subscription plan to pay-as-you-go
            # credits (402 insufficient_credits, observed 2026-09-15): the plan gap is
            # named and never retried, the way a region 403 is its own refusal.
            verdict["refusal"] = "model_not_in_plan"
        elif exc.code == 403:
            verdict["refusal"] = region_optin_refusal(exc) or verdict["refusal"]
        return None, verdict
    except ResponseTooLarge as exc:
        verdict["refusal"] = str(exc)
        return None, verdict
    except Exception as exc:
        verdict["refusal"] = "transport_error: " + type(exc).__name__
        return None, verdict
    try:
        native_text = json.dumps(response, indent=2, sort_keys=True) + "\n"
    except (TypeError, ValueError):
        verdict["refusal"] = "native response was not a JSON object"
        return None, verdict
    with (attempt_dir / "native.json").open("xb") as stream:
        stream.write(native_text.encode())
    # ClinePass wraps the chat-completions document as {"data": {...}}; unwrap when
    # present so qualification, usage and finish reason read the inner document, and
    # record the response's provider field in the verdict (brief 15, G1).
    response, response_provider = unwrap_data(response)
    verdict["provider"] = response_provider
    if isinstance(response, dict):
        verdict["usage"] = response.get("usage")
        choices = response.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            verdict["finish_reason"] = choices[0].get("finish_reason")
    problem = qualify(response, sent_model, aliases=(request["model"],))
    if problem:
        verdict["refusal"] = reasoning_overrun(response, max_tokens) or problem
        return None, verdict
    markup = tool_markup_refusal(_content_of(response))
    if markup:
        verdict["refusal"] = markup
        return None, verdict
    name, problem = expected_artifact(work)
    if problem:
        verdict["refusal"] = problem
        return None, verdict
    content = response["choices"][0]["message"]["content"]
    if name == "reply.txt":
        # Reply-only tasks (reviews) publish the response text itself; no code schema applies.
        code = content.strip()
    else:
        code, problem = expected_code(content)
        if problem:
            verdict["refusal"] = problem
            return None, verdict
    data = code.encode()
    output = Path(request["output_directory"])
    target = output / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    receipt = {
        "status": "completed",
        "finish_reason": "stop",
        "actual_model": request["model"],
        "manifest_sha256": request["manifest_sha256"],
        "artifacts": [{"path": name, "sha256": sha256_digest(data)}],
    }
    return receipt, verdict


def sha256_digest(data):
    import hashlib

    return hashlib.sha256(data).hexdigest()
