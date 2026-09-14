"""evaluation: the scorecard as a document, plus the per-account readiness view."""

import time
import uuid

import pytest

from inference_grid.evaluation import (
    evaluation_document,
    write_document,
    write_section,
)
from inference_grid.ledger import Ledger, digest


def receipt(model, manifest):
    return {
        "status": "completed",
        "finish_reason": "stop",
        "actual_model": model,
        "manifest_sha256": manifest,
        "artifacts": [{"path": "out.py", "sha256": "a" * 64}],
    }


def completed_attempt(ledger, account, task_id, family, model):
    spec = {
        "authorized": True,
        "model": model,
        "family": family,
        "argv": ["/usr/bin/true"],
        "workspace": "/tmp/ws-" + uuid.uuid4().hex[:8],
        "timeout": 60,
        "output_bytes": 1000,
        "inputs": {},
        "manifest_sha256": digest({}),
    }
    ledger.submit(task_id, "project", spec)
    aid, generation = ledger.claim(task_id, account, {"five_hour": 0.01, "weekly": 0.01})
    ledger.start(aid, generation)
    ledger.finish(aid, generation, receipt(model, spec["manifest_sha256"]))
    return aid


def make_ledger(tmp_path):
    ledger = Ledger("sqlite:///" + str(tmp_path / "ledger.sqlite"))
    ledger.initialize()
    ledger.configure_account(
        "acct",
        2,
        {"five_hour": 10, "weekly": 20},
        time.time() + 600,
        ["glm-5.3-flash", "kimi-k3"],
    )
    return ledger


def test_scorecard_rows_render_rates_means_and_order(tmp_path):
    ledger = make_ledger(tmp_path)
    # glm: three attempts, two accepted, usage on every outcome (301..303 in, 1001..1003 out).
    for i, ok in ((1, True), (2, True), (3, False)):
        aid = completed_attempt(ledger, "acct", f"g{i}", "glm", "glm-5.3-flash")
        ledger.record_outcome(aid, "pure_function", ok, usage={"input": 300 + i, "output": 1000 + i})
    # kimi: one unaccepted attempt with no usage, in a different category and family.
    aid = completed_attempt(ledger, "acct", "k1", "kimi", "kimi-k3")
    ledger.record_outcome(aid, "independent_review", False)
    document = evaluation_document(ledger, now=1_700_000_000)
    lines = document.splitlines()
    assert lines[0] == "# Evaluation"
    header = next(line for line in lines if line.startswith("| family"))
    assert all(
        column in header
        for column in ("attempts", "completed", "accepted", "acceptance rate", "last attempt")
    )
    glm = next(line for line in lines if line.startswith("| glm "))
    kimi = next(line for line in lines if line.startswith("| kimi "))
    assert "| 3 | 3 | 2 | 67% | 0 | 0 | 302 | 1002 |" in glm
    assert "—" not in glm  # usage recorded and a last attempt time exists
    assert "| 1 | 1 | 0 | 0% | 0 | 0 | — | — |" in kimi
    assert lines.index(glm) < lines.index(kimi)  # sorted by family, model, category


def test_account_readiness_renders_one_row_per_alias(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.configure_account(
        "acct2",
        3,
        {"five_hour": 10, "weekly": 20},
        time.time() + 600,
        ["kimi-k3"],
        ["go-alias"],
    )
    document = evaluation_document(ledger)
    row = next(line for line in document.splitlines() if line.startswith("| go-alias "))
    assert "| go-alias | acct2 | 0/3 | fresh (" in row
    assert "kimi-k3" in row
    # An account whose quota window has passed at render time still renders, stale.
    stale_at = time.time() + 700
    stale = next(
        line
        for line in evaluation_document(ledger, now=stale_at).splitlines()
        if line.startswith("| go-alias ")
    )
    assert "| go-alias | acct2 | 0/3 | stale (" in stale


def test_empty_ledger_renders_headers_plus_seeded_placeholders(tmp_path):
    # A fresh ledger has no scorecard rows, but the seeded cline alias placeholder is
    # named in the readiness view: an unconfigured account is a fact, not silence.
    ledger = Ledger("sqlite:///" + str(tmp_path / "empty.sqlite"))
    ledger.initialize()
    document = evaluation_document(ledger)
    assert "| family | model | category" in document
    assert "| alias | account |" in document
    rows = [
        line
        for line in document.splitlines()
        if line.startswith("| ") and "---" not in line and not line.startswith("| family")
        and not line.startswith("| alias")
    ]
    assert rows == ["| cline | cline | 0/1 | stale (—) | never | 0 |  |"]


def test_write_document_refuses_docs_and_writes_elsewhere(tmp_path):
    with pytest.raises(ValueError, match="docs"):
        write_document("# x\n", tmp_path / "docs" / "EVALUATION.md")
    out = tmp_path / "rendered" / "evaluation.md"
    path = write_document("# x\n", out)
    assert path.read_text() == "# x\n"


def test_replace_section_splices_only_that_section(tmp_path):
    # The one sanctioned docs/ write: the scorecard section is replaced in place and the
    # hand-written parts survive byte-identical.
    target = tmp_path / "docs" / "EVALUATION.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "# Evaluation notes\n\n"
        "Hand-written intro stays.\n\n"
        "## Scorecard\n\n"
        "| old | rows |\n"
        "| --- | --- |\n"
        "| x | 1 |\n\n"
        "## Account readiness\n\n"
        "Hand-written account notes.\n"
    )
    ledger = make_ledger(tmp_path)
    aid = completed_attempt(ledger, "acct", "g1", "glm", "glm-5.3-flash")
    ledger.record_outcome(aid, "pure_function", True, usage={"input": 300, "output": 1200})
    path = write_section(evaluation_document(ledger), target, "## Scorecard")
    text = path.read_text()
    assert "Hand-written intro stays." in text
    assert "Hand-written account notes." in text
    assert "| old | rows |" not in text
    assert text.count("## Scorecard") == 1
    assert "| family | model | category" in text
    assert "| 1 | 1 | 1 | 100% |" in text
    before, after = text.split("## Scorecard", 1)
    assert before == "# Evaluation notes\n\nHand-written intro stays.\n\n"
    assert after.startswith("\n\n| family | model | category")
    assert after.endswith("Hand-written account notes.\n")


def test_an_absent_section_is_appended_and_an_unknown_section_is_refused(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("# Notes\n\nOnly prose.\n")
    write_section(evaluation_document(make_ledger(tmp_path)), target, "## Scorecard")
    text = target.read_text()
    assert text.startswith("# Notes\n\nOnly prose.\n")
    assert "## Scorecard" in text and "| family | model | category" in text
    with pytest.raises(ValueError, match="no '## Missing' section"):
        write_section(evaluation_document(make_ledger(tmp_path)), target, "## Missing")


def test_qualification_matrix_prints_threshold_verdicts(tmp_path):
    ledger = make_ledger(tmp_path)
    aid = completed_attempt(ledger, "acct", "g1", "glm", "glm-5.3-flash")
    ledger.record_outcome(aid, "pure_function", True, usage={"input": 300, "output": 1200})
    document = evaluation_document(ledger)
    scorecard_rows = [
        line
        for line in document.splitlines()
        if line.startswith("| glm | glm-5.3-flash | pure_function |")
        and " | no |" not in line
    ]
    assert len(scorecard_rows) == 1
    assert scorecard_rows[0].startswith(
        "| glm | glm-5.3-flash | pure_function | 1 | 1 | 1 | 100% | 0 | 0 | 300 | 1200 | "
    )
    matrix_rows = [
        line
        for line in document.splitlines()
        if line.startswith("| glm | glm-5.3-flash | pure_function |")
        and line.endswith("| no |")
    ]
    assert len(matrix_rows) == 1  # 1 accepted pure_function row: below the threshold of 2
