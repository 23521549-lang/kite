"""Gói 5 (integration, agent trưởng): skill scanner adapters chosen by worker (a2)'s
evaluation — skillshield 1.3.0 (main gate, 4/4) + Argus 0.2.1 (mandatory companion,
the only candidate catching plain-language exfiltration) as the pair gate
"skillshield+argus"; Argus -t over tools/list dumps in the MCP scan paths.

Fixtures below are synthetic canned JSON in the real output shapes (neutral test
messages — no poisoned payload text). The live behaviour was measured by worker
(a2) on the 4 research samples; see
~/workspace/kite-ui-build/goi-5-research/BAO-CAO-DANH-GIA.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from poc.kaggle_worker import connect as c

NOW = 1_795_400_000.0

SHIELD_CLEAN = {
    "total": 1, "flagged": 0, "critical": 0, "errors": 0, "flag_rate": 0.0,
    "unscanned": [],
    "results": [{"skill": "SKILL.md", "risk_level": "SAFE", "risk_score": 0.0,
                 "findings": []}],
}
SHIELD_FLAGGED = {
    "total": 1, "flagged": 1, "critical": 1, "errors": 0, "flag_rate": 1.0,
    "unscanned": [],
    "results": [{"skill": "SKILL.md", "risk_level": "CRITICAL", "risk_score": 40,
                 "findings": [{"id": "SG-001", "name": "Prompt Injection",
                               "severity": "critical",
                               "description": "Test finding: instruction override.",
                               "snippets": []}]}],
}
SHIELD_UNSCANNED = {
    "total": 1, "flagged": 0, "critical": 0, "errors": 0, "flag_rate": 0.0,
    "unscanned": ["notes/private.md"],
    "results": [{"skill": "SKILL.md", "risk_level": "SAFE", "risk_score": 0.0,
                 "findings": []}],
}

ARGUS_CLEAN = {
    "tool": {"name": "mcpscan", "version": "0.2.1"},
    "summary": {"findings": 0, "counts": {"critical": 0, "high": 0, "medium": 0,
                                          "low": 0, "info": 0}},
    "findings": [],
}
ARGUS_FLAGGED = {
    "tool": {"name": "mcpscan", "version": "0.2.1"},
    "summary": {"findings": 1, "counts": {"critical": 1, "high": 0, "medium": 0,
                                          "low": 0, "info": 0}},
    "findings": [{"ruleId": "MCP003", "rule": "exfiltration", "severity": "critical",
                  "title": "Exfiltration pattern",
                  "message": "Test finding: data exfiltration pattern.",
                  "target": "tool:add", "evidence": "test"}],
}
ARGUS_MEDIUM_ONLY = {
    "tool": {"name": "mcpscan", "version": "0.2.1"},
    "summary": {"findings": 1, "counts": {"critical": 0, "high": 0, "medium": 1,
                                          "low": 0, "info": 0}},
    "findings": [{"ruleId": "MCP010", "rule": "test-rule", "severity": "medium",
                  "title": "Test medium finding",
                  "message": "Test finding: medium severity only.",
                  "target": "tool:add", "evidence": "test"}],
}


def _runner(payload, returncode=0):
    def run(command, timeout):
        return returncode, payload, ""
    return run


# --------------------------------------------------------------------- skillshield adapter

def test_skillshield_clean_passes(tmp_path):
    report = c.SkillShieldScanner(runner=_runner(json.dumps(SHIELD_CLEAN))).scan(tmp_path)
    assert report.verdict == "pass"
    assert report.findings == []
    assert report.scanner == "skillshield"
    assert report.scanner_version == "1.3.0"


def test_skillshield_flagged_fails(tmp_path):
    report = c.SkillShieldScanner(
        runner=_runner(json.dumps(SHIELD_FLAGGED), returncode=1)).scan(tmp_path)
    assert report.verdict == "fail"
    assert report.findings[0].rule == "SG-001"
    assert report.findings[0].severity == "critical"


def test_skillshield_unscanned_files_fail(tmp_path):
    report = c.SkillShieldScanner(
        runner=_runner(json.dumps(SHIELD_UNSCANNED))).scan(tmp_path)
    assert report.verdict == "fail"
    assert any(f.rule == "unscanned-files" for f in report.findings)


def test_skillshield_bad_exit_is_an_error(tmp_path):
    with pytest.raises(c.ConnectError):
        c.SkillShieldScanner(runner=_runner("", returncode=2)).scan(tmp_path)


def test_skillshield_missing_binary_fails_closed(tmp_path):
    def missing(command, timeout):
        raise c.ScannerMissingError("không tìm thấy binary 'skillshield'")
    with pytest.raises(c.ScannerMissingError):
        c.SkillShieldScanner(runner=missing).scan(tmp_path)


# --------------------------------------------------------------------- argus adapter

def test_argus_clean_passes(tmp_path):
    report = c.ArgusScanner(runner=_runner(json.dumps(ARGUS_CLEAN))).scan(tmp_path)
    assert report.verdict == "pass"
    assert report.scanner == "argus"


def test_argus_flagged_fails(tmp_path):
    report = c.ArgusScanner(
        runner=_runner(json.dumps(ARGUS_FLAGGED), returncode=1)).scan(tmp_path)
    assert report.verdict == "fail"
    assert report.findings[0].rule == "MCP003"
    assert report.findings[0].severity == "critical"


def test_argus_exit_1_with_only_medium_still_fails(tmp_path):
    # Argus's own gate tripped (exit 1) but no single finding is critical/high:
    # the trip must stay visible so the scan fails instead of passing.
    report = c.ArgusScanner(
        runner=_runner(json.dumps(ARGUS_MEDIUM_ONLY), returncode=1)).scan(tmp_path)
    assert report.verdict == "fail"
    assert any(f.rule == "argus-flagged" for f in report.findings)


def test_argus_missing_binary_fails_closed(tmp_path):
    def missing(command, timeout):
        raise c.ScannerMissingError("không tìm thấy binary 'npx'")
    with pytest.raises(c.ScannerMissingError):
        c.ArgusScanner(runner=missing).scan(tmp_path)


def test_argus_tools_dump_shape(tmp_path):
    seen = {}

    def run(command, timeout):
        dump_path = command[command.index("-t") + 1]
        seen["dump"] = json.loads(Path(dump_path).read_text())
        return 0, json.dumps(ARGUS_CLEAN), ""

    findings, _version, failed = c.ArgusScanner(runner=run).scan_tools(
        [{"name": "add", "description": "Add two numbers.",
          "inputSchema": {"type": "object"}}])
    assert seen["dump"] == {"tools": [{"name": "add", "description": "Add two numbers.",
                                       "inputSchema": {"type": "object"}}]}
    assert findings == [] and failed is False


# --------------------------------------------------------------------- pair gate

def test_pair_passes_only_when_both_clean(tmp_path):
    pair = c.SkillPairScanner(
        shield=c.SkillShieldScanner(runner=_runner(json.dumps(SHIELD_CLEAN))),
        argus=c.ArgusScanner(runner=_runner(json.dumps(ARGUS_CLEAN))))
    report = pair.scan(tmp_path)
    assert report.verdict == "pass"
    assert report.scanner == "skillshield+argus"


def test_pair_fails_when_shield_flags(tmp_path):
    pair = c.SkillPairScanner(
        shield=c.SkillShieldScanner(
            runner=_runner(json.dumps(SHIELD_FLAGGED), returncode=1)),
        argus=c.ArgusScanner(runner=_runner(json.dumps(ARGUS_CLEAN))))
    report = pair.scan(tmp_path)
    assert report.verdict == "fail"
    assert any(f.rule == "SG-001" for f in report.findings)


def test_pair_fails_when_argus_flags(tmp_path):
    pair = c.SkillPairScanner(
        shield=c.SkillShieldScanner(runner=_runner(json.dumps(SHIELD_CLEAN))),
        argus=c.ArgusScanner(
            runner=_runner(json.dumps(ARGUS_FLAGGED), returncode=1)))
    report = pair.scan(tmp_path)
    assert report.verdict == "fail"
    assert any(f.rule == "MCP003" for f in report.findings)


def test_builtin_skill_scanners_registered():
    assert c.DEFAULT_SKILL_SCANNER == "skillshield+argus"
    assert set(c.skill_scanner_names()) >= {"skillshield", "argus", "skillshield+argus"}


# --------------------------------------------------------------------- argus -t inside MCP scans

def _mcpiranha_ok(command, timeout):
    return 0, json.dumps({"findings": [], "scanner_version": "0.4.1"}), ""


def _stdio_ok(command, payload, timeout):
    transcript = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": [
        {"name": "add", "description": "Add two numbers.",
         "inputSchema": {"type": "object"}}]}})
    return 0, transcript + "\n", ""


def test_scan_source_runs_argus_on_tools_dump(tmp_path):
    tool = {"id": "tool-src", "kind": "source", "enabled": False,
            "config": {"path": str(tmp_path),
                       "command": ["python3", "server.py"]}, "last_scan": None}
    report = c.scan_tool_config(
        tool, state_dir=tmp_path, now=NOW, mcpiranha_runner=_mcpiranha_ok,
        runner=_stdio_ok,
        argus_runner=_runner(json.dumps(ARGUS_FLAGGED), returncode=1))
    assert report.verdict == "fail"
    assert any(f.rule == "MCP003" and f.scanner == "argus" for f in report.findings)
    assert "+argus" in report.scanner
    assert report.tools == ["add"]


def test_scan_source_argus_missing_fails_closed(tmp_path):
    def missing(command, timeout):
        raise c.ScannerMissingError("không tìm thấy binary 'npx'")

    tool = {"id": "tool-src", "kind": "source", "enabled": False,
            "config": {"path": str(tmp_path),
                       "command": ["python3", "server.py"]}, "last_scan": None}
    report = c.scan_tool_config(
        tool, state_dir=tmp_path, now=NOW, mcpiranha_runner=_mcpiranha_ok,
        runner=_stdio_ok, argus_runner=missing)
    assert report.verdict == "fail"
    assert any(f.rule == "scanner-missing" for f in report.findings)
