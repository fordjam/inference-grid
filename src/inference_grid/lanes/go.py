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

ENDPOINT = "https://opencode.ai/zen/go/v1/chat/completions"
USER_AGENT = "inference-grid-lane/1.0"
MAX_BODY = 4 * 1024 * 1024
MAX_CONTENT = 40000


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
    entry = document.get("opencode-go") if isinstance(document, dict) else None
    key = entry.get("key") if isinstance(entry, dict) else None
    if not isinstance(key, str) or not key:
        return None, "credential opencode-go key is empty"
    return key, None


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


def http_send(body, key, session, timeout):
    """POST one chat completion; return the parsed JSON document."""
    request = urllib.request.Request(
        ENDPOINT,
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


def qualify(response, model):
    """The reply must come from the asked model, have stopped normally, and carry text."""
    if not isinstance(response, dict):
        return "native response was not a JSON object"
    if response.get("model") != model:
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
        "usage": None,
        "finish_reason": None,
        "refusal": None,
    }
    key, problem = read_key(lane["credential_path"])
    if problem:
        verdict["refusal"] = problem
        return None, verdict
    # The transport waits as long as the lane budget allows, bounded: reviews at 16k output
    # tokens outran the old fixed 150 s. An explicit timeout (tests) always wins.
    transport_timeout = timeout if timeout is not None else min(lane["wall_seconds"], 600)
    verdict["transport_timeout"] = transport_timeout
    body = {
        "model": request["model"],
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "max_tokens": max_tokens,
    }
    try:
        if send is not None:
            response = send(body, key, session, transport_timeout)
        else:
            response = http_send(body, key, session, transport_timeout)
    except urllib.error.HTTPError as exc:
        # The status code and a fixed reason only; the error body is never read or recorded.
        verdict["refusal"] = f"endpoint returned HTTP {exc.code}"
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
    if isinstance(response, dict):
        verdict["usage"] = response.get("usage")
        choices = response.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            verdict["finish_reason"] = choices[0].get("finish_reason")
    problem = qualify(response, request["model"])
    if problem:
        verdict["refusal"] = problem
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
