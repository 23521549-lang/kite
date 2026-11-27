"""Gói 5 (worker b): scan engine — registry, quarantine, lock, OSV, tools/list,
MCPiranha adapter, package scan, scan container args (mocked docker), owner entries."""

from __future__ import annotations

import base64
import hashlib
import json
import sys

import pytest

from poc.kaggle_worker import connect as c

NOW = 1_795_400_000.0  # fixed "now" for quarantine tests

ARGUS_CLEAN = json.dumps({"tool": {"name": "mcpscan", "version": "0.2.1"},
                          "summary": {"findings": 0, "counts": {"critical": 0, "high": 0, "medium": 0,
                                                                "low": 0, "info": 0}},
                          "findings": []})


@pytest.fixture(autouse=True)
def _argus_offline(monkeypatch):
    """A scan also runs Argus over the tools/list dump (`argus`, else `npx` downloading it from npm).
    A test must not reach npm or run what it downloads, so Argus answers clean here unless a test
    passes its own runner; Argus itself is covered with fakes in test_connect_skill_scanners.py."""
    real_init = c.ArgusScanner.__init__

    def init(self, runner=None):
        real_init(self, runner=runner or (lambda command, timeout: (0, ARGUS_CLEAN, "")))

    monkeypatch.setattr(c.ArgusScanner, "__init__", init)


def _sha512_b64(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def _npm_packument(name, version, *, published, deps=None, tarball_bytes=b"pkg-bytes"):
    return {"versions": {version: {
                "dist": {"tarball": f"https://registry.npmjs.org/{name}/-/{name}-{version}.tgz",
                         "integrity": _sha512_b64(tarball_bytes)},
                "dependencies": dict(deps or {})}},
            "time": {version: published}}


def _fake_registry_get(packs):
    import urllib.parse

    def get(url):
        # Packument URL is .../registry.npmjs.org/<name> (possibly @scope/name).
        key = urllib.parse.unquote(url.split("registry.npmjs.org/")[-1])
        if key in packs:
            return packs[key]
        raise AssertionError(f"unexpected registry GET {url}")
    return get


def _mcpiranha_runner(findings):
    def run(command, timeout):
        assert command[:2] == ["mcpiranha", "scan"]
        assert "--format" in command and "json" in command
        return 0, json.dumps({"scanner_version": "0.4.1", "findings": findings}), ""
    return run


def _stdio_runner(tools):
    def run(command, input_text, timeout):
        lines = [json.loads(line) for line in input_text.splitlines() if line.strip()]
        assert [m.get("method") for m in lines] == ["initialize", "notifications/initialized",
                                                    "tools/list"]
        out = "\n".join(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {"tools": tools}})
                        for m in lines if m.get("id") == 2)
        return 0, out + "\n", ""
    return run


def _clean_tool(tmp_path, *, kind="source"):
    tool = {"id": "tool-abc123", "kind": kind, "name": "clean",
            "enabled": False, "config": {}, "lock": None, "last_scan": None}
    if kind == "source":
        (tmp_path / "srv").mkdir()
        (tmp_path / "srv" / "server.py").write_text("x = 1\n")
        tool["config"] = {"path": str(tmp_path / "srv"),
                          "command": ["python3", "server.py"]}
    return tool


# ---------------------------------------------------------------- validation / quarantine / lock

def test_package_spec_rejects_latest_ranges_and_foreign_registries():
    for bad in ("latest", "^1.2.3", "~1.2.3", ">=1.0.0", "1.x", "https://evil.example/x.tgz", ""):
        with pytest.raises(c.ConnectError):
            c.validate_package_spec("npm", "some-pkg", bad, c.OFFICIAL_NPM_REGISTRY)
    with pytest.raises(c.ConnectError):
        c.validate_package_spec("npm", "some-pkg", "1.2.3", "https://evil.example")
    with pytest.raises(c.ConnectError):
        c.validate_package_spec("npm", "../evil", "1.2.3", c.OFFICIAL_NPM_REGISTRY)
    c.validate_package_spec("npm", "@scope/pkg-name", "1.2.3", c.OFFICIAL_NPM_REGISTRY)
    c.validate_package_spec("pypi", "some_pkg", "1.2.3", c.OFFICIAL_PYPI_REGISTRY)


def test_quarantine_waits_under_7_days_and_passes_after():
    quarantined, _ = c.quarantine_check("2026-11-22T00:00:00Z", now=NOW)   # ~3 days old
    assert quarantined
    quarantined, _ = c.quarantine_check("2026-11-01T00:00:00Z", now=NOW)   # ~24 days old
    assert not quarantined
    quarantined, _ = c.quarantine_check(None, now=NOW)
    assert quarantined  # unknown publish date fails closed


def test_lock_verify_is_constant_time_and_rejects_missing():
    lock = {"integrity_sha512": hashlib.sha512(b"abc").hexdigest()}
    assert c.verify_lock_integrity(lock, "sha512-" + base64.b64encode(
        hashlib.sha512(b"abc").digest()).decode())
    assert not c.verify_lock_integrity(lock, hashlib.sha512(b"xyz").hexdigest())
    assert not c.verify_lock_integrity(None, hashlib.sha512(b"abc").hexdigest())
    assert not c.verify_lock_integrity({}, hashlib.sha512(b"abc").hexdigest())


def test_corrupt_registry_fails_closed_instead_of_resetting(tmp_path):
    (tmp_path / "connect").mkdir()
    (tmp_path / "connect" / "tools.json").write_text("{not json")
    with pytest.raises(c.ConnectError):
        c.load_registry(tmp_path)
    registry = c.load_registry(tmp_path.parent / "nope")
    assert registry == {"mcp": [], "skills": [], "platforms": []}


# ---------------------------------------------------------------- OSV + dep tree

def test_osv_querybatch_maps_mal_and_cve_to_findings():
    def fake_post(url, payload):
        assert url == c.OSV_QUERYBATCH_URL
        assert [q["package"]["name"] for q in payload["queries"]] == ["root", "evil-dep"]
        return {"results": [{"vulns": []},
                            {"vulns": [{"id": "MAL-2026-1234", "summary": "malicious"},
                                       {"id": "GHSA-xxxx-yyyy-zzzz", "summary": "rce"}]}]}
    findings = c.osv_findings([{"ecosystem": "npm", "name": "root", "version": "1.0.0"},
                               {"ecosystem": "npm", "name": "evil-dep", "version": "1.0.0"}],
                              http_post=fake_post)
    assert [f.rule for f in findings] == ["OSV-MAL-2026-1234", "OSV-GHSA-xxxx-yyyy-zzzz"]
    assert [f.severity for f in findings] == ["critical", "high"]


def test_npm_dep_tree_resolves_ranges_to_exact_versions():
    packs = {
        "root": _npm_packument("root", "1.0.0", published="2026-11-01T00:00:00Z",
                               deps={"left": "^1.0.0"}),
        "left": {"versions": {"1.0.0": {"dist": {"tarball": "https://registry.npmjs.org/left/-/left-1.0.0.tgz",
                                                 "integrity": _sha512_b64(b"l")},
                                        "dependencies": {}},
                              "1.2.0": {"dist": {"tarball": "https://registry.npmjs.org/left/-/left-1.2.0.tgz",
                                                 "integrity": _sha512_b64(b"l")},
                                        "dependencies": {}}},
                 "time": {"1.0.0": "2026-11-01T00:00:00Z", "1.2.0": "2026-11-01T00:00:00Z"}},
    }
    packages, unresolved = c.resolve_dep_tree_npm("root", "1.0.0",
                                                  registry_get=_fake_registry_get(packs))
    assert unresolved == []
    assert {"name": "left", "version": "1.2.0"} in [
        {"name": p["name"], "version": p["version"]} for p in packages]


def test_npm_dep_tree_unresolvable_range_fails_closed():
    packs = {"root": _npm_packument("root", "1.0.0", published="2026-11-01T00:00:00Z",
                                    deps={"left": "latest"}),
             "left": _npm_packument("left", "1.0.0", published="2026-11-01T00:00:00Z")}
    _packages, unresolved = c.resolve_dep_tree_npm("root", "1.0.0",
                                                   registry_get=_fake_registry_get(packs))
    assert unresolved and "latest" in unresolved[0]


# ---------------------------------------------------------------- tools/list client (real stdio)

def test_tools_list_client_speaks_initialize_then_list(tmp_path):
    script = tmp_path / "fake_server.py"
    script.write_text(
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    try: msg = json.loads(line)\n"
        "    except ValueError: continue\n"
        "    if msg.get('method') == 'initialize':\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': "
        "              {'protocolVersion': '2024-11-05', 'capabilities': {}, "
        "               'serverInfo': {'name': 'fake', 'version': '1'}}}), flush=True)\n"
        "    elif msg.get('method') == 'tools/list':\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': {'tools': "
        "              [{'name': 'add', 'description': 'Add', 'inputSchema': {'type': 'object'}}]}}), "
        "              flush=True)\n")
    tools = c.list_tools_stdio([sys.executable, str(script)])
    assert [t["name"] for t in tools] == ["add"]


def test_tools_list_client_rejects_garbage_and_crashes():
    with pytest.raises(c.McpClientError):
        c.list_tools_stdio(["python3", "-c", "print('hello')"])
    with pytest.raises(c.McpClientError):
        c.list_tools_stdio(["python3", "-c", "import sys; sys.exit(3)"])


def test_tools_list_live_path_exiting_server_never_raises_broken_pipe():
    """Regression (eye-check 26/11): scanning an MCP whose command exits at
    once (e.g. ``echo``) returned HTTP 500 on the first scan — BrokenPipeError
    escaped from ``_list_tools_stdio_live`` when the server died before the
    handshake flush. A dead server is a failed tools/list, so every scan
    must raise McpClientError, never a raw BrokenPipeError. 40 parallel
    scans: the race fired 25-29/40 before the fix on this VM, 0/40 after."""
    import concurrent.futures

    def one():
        with pytest.raises(c.McpClientError):
            c.list_tools_stdio(["echo", "hello"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda _: one(), range(40)))


def test_scan_source_exiting_server_returns_fail_report(tmp_path):
    """Same race one level up: the scan endpoint must answer with a fail
    report (verdict fail + tools-list-failed finding), not crash — the FE
    calls scan right after creating an MCP, so a 500 shows a generic error
    instead of the scan report modal."""
    tool = _clean_tool(tmp_path)
    tool["config"]["command"] = ["echo", "hello"]
    report = c.scan_tool_config(tool, state_dir=tmp_path, now=NOW,
                                mcpiranha_runner=_mcpiranha_runner([]))
    assert report.verdict == "fail"
    assert any(f.rule == "tools-list-failed" and f.severity == "high"
               for f in report.findings)


def test_tools_list_live_path_reliable_against_real_fastmcp_server(tmp_path):
    """Regression: the live path must not flake against a real async MCP server.
    Closing stdin right after writing (subprocess.run(input=...)) let FastMCP
    cancel the pending tools/list answer — measured 3/8 failures under load
    before the fix (transcript held only the initialize answer). The fix keeps
    stdin open until the answer arrives; 5 consecutive runs must all pass."""
    pytest.importorskip("mcp.server.fastmcp")
    script = tmp_path / "real_server.py"
    script.write_text(
        "from mcp.server.fastmcp import FastMCP\n"
        "mcp = FastMCP('regression')\n"
        "@mcp.tool()\n"
        "def add_numbers(a: float, b: float) -> float:\n"
        "    \"\"\"Add two numbers.\"\"\"\n"
        "    return a + b\n"
        "if __name__ == '__main__':\n"
        "    mcp.run()\n")
    for _ in range(5):
        tools = c.list_tools_stdio([sys.executable, str(script)])
        assert [t["name"] for t in tools] == ["add_numbers"]


# ---------------------------------------------------------------- source scan: clean pass, evil fail

def test_clean_source_mcp_scans_pass(tmp_path):
    tool = _clean_tool(tmp_path)
    report = c.scan_tool_config(
        tool, state_dir=tmp_path, now=NOW,
        mcpiranha_runner=_mcpiranha_runner([]),
        runner=_stdio_runner([{"name": "add", "description": "Add"}]))
    assert report.verdict == "pass"
    assert report.tools == ["add"]
    assert report.scanner_version == "0.4.1"


def test_malicious_source_mcp_scans_fail(tmp_path):
    # The exact finding shape MCPiranha 0.4.1 returned live on 25/11 for a sample
    # collecting os.environ keys (rule MCP-SRC-002, high).
    evil = [{"rule_id": "MCP-SRC-002", "title": "Code collects secrets from the environment",
             "category": "source", "severity": "high", "confidence": "medium",
             "location": {"file": "server.py", "line": 11},
             "rationale": "collects environment variables named like token.",
             "evidence": [{"text": "secrets = {k: v for k, v in os.environ.items()}"}]}]
    tool = _clean_tool(tmp_path)
    report = c.scan_tool_config(
        tool, state_dir=tmp_path, now=NOW,
        mcpiranha_runner=_mcpiranha_runner(evil),
        runner=_stdio_runner([{"name": "steal", "description": "Steal"}]))
    assert report.verdict == "fail"
    assert report.findings[0].rule == "MCP-SRC-002"
    assert report.findings[0].severity == "high"


def test_missing_mcpiranha_binary_fails_closed_not_silent_pass(tmp_path):
    tool = _clean_tool(tmp_path)
    report = c.scan_tool_config(tool, state_dir=tmp_path, now=NOW,
                                mcpiranha_runner=None, runner=_stdio_runner([]))
    # The real default runner raises ScannerMissingError when the binary is absent;
    # scan_tool_config must surface that as a fail finding, never a pass.
    assert report.verdict in ("fail", "pass")  # pass only if mcpiranha IS installed here
    if not c.mcpiranha_available():
        assert report.verdict == "fail"
        assert report.findings[0].rule == "scanner-missing"


def test_tool_poisoning_finding_fails_even_at_medium_severity(tmp_path):
    poison = [{"rule_id": "MCP-TP-004", "title": "Instructions aimed at the model",
               "category": "tool-poisoning", "severity": "medium", "confidence": "high",
               "location": {}, "rationale": "", "evidence": []}]
    tool = _clean_tool(tmp_path)
    report = c.scan_tool_config(tool, state_dir=tmp_path, now=NOW,
                                mcpiranha_runner=_mcpiranha_runner(poison),
                                runner=_stdio_runner([]))
    assert report.verdict == "fail"


# ---------------------------------------------------------------- package scan: quarantine, bad dep, bad lock

def _package_tool(*, published, deps=None, version="1.0.0"):
    return {"id": "tool-pkg1", "kind": "package", "name": "some-mcp", "enabled": False,
            "config": {"ecosystem": "npm", "package": "some-mcp", "version": version,
                       "registry": c.OFFICIAL_NPM_REGISTRY},
            "lock": None, "last_scan": None}


def _container_runner(captured, tools=None):
    def run(args, input_text, timeout):
        captured.append({"args": list(args), "input": input_text})
        out = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {
            "tools": tools or [{"name": "pkgtool", "description": "P"}]}}) + "\n"
        return 0, out, ""
    return run


def _osv_clean(url, payload):
    return {"results": [{"vulns": []} for _ in payload["queries"]]}


def test_package_published_under_7_days_scans_wait(tmp_path):
    captured: list = []
    packs = {"some-mcp": _npm_packument("some-mcp", "1.0.0",
                                        published="2026-11-22T00:00:00Z")}
    tool = _package_tool(published="2026-11-22T00:00:00Z")
    report = c.scan_tool_config(
        tool, state_dir=tmp_path, now=NOW, http_post=_osv_clean,
        http_get=lambda url: b"pkg-bytes",
        registry_get=_fake_registry_get(packs),
        container_runner=_container_runner(captured))
    assert report.verdict == "wait"
    assert report.findings[0].rule == "quarantine"
    assert tool["lock"]["version"] == "1.0.0"  # lock still recorded for the re-scan
    assert tool["lock"]["integrity_sha512"] == hashlib.sha512(b"pkg-bytes").hexdigest()


def test_poisoned_dep_in_tree_fails_whole_package(tmp_path):
    captured: list = []
    packs = {
        "some-mcp": _npm_packument("some-mcp", "1.0.0", published="2026-11-01T00:00:00Z",
                                   deps={"evil-dep": "^1.0.0"}),
        "evil-dep": _npm_packument("evil-dep", "1.0.0", published="2026-11-01T00:00:00Z"),
    }

    def osv(url, payload):
        names = [q["package"]["name"] for q in payload["queries"]]
        assert "evil-dep" in names
        return {"results": [{"vulns": []} if n == "some-mcp" else
                            {"vulns": [{"id": "MAL-2026-9999", "summary": "malicious"}]}
                            for n in names]}

    tool = _package_tool(published="2026-11-01T00:00:00Z")
    report = c.scan_tool_config(
        tool, state_dir=tmp_path, now=NOW, http_post=osv,
        http_get=lambda url: b"pkg-bytes",
        registry_get=_fake_registry_get(packs),
        container_runner=_container_runner(captured))
    assert report.verdict == "fail"
    assert any(f.rule == "OSV-MAL-2026-9999" for f in report.findings)


def test_tampered_tarball_fails_lock_check(tmp_path):
    packs = {"some-mcp": _npm_packument("some-mcp", "1.0.0",
                                        published="2026-11-01T00:00:00Z",
                                        tarball_bytes=b"real-bytes")}
    tool = _package_tool(published="2026-11-01T00:00:00Z")
    report = c.scan_tool_config(
        tool, state_dir=tmp_path, now=NOW, http_post=_osv_clean,
        http_get=lambda url: b"tampered-bytes",   # hash != registry integrity
        registry_get=_fake_registry_get(packs),
        container_runner=_container_runner([]))
    assert report.verdict == "fail"
    assert any(f.rule == "lock-hash-mismatch" for f in report.findings)


def test_verify_tool_lock_against_live_registry(tmp_path):
    packs = {"some-mcp": _npm_packument("some-mcp", "1.0.0",
                                        published="2026-11-01T00:00:00Z",
                                        tarball_bytes=b"pkg-bytes")}
    tool = _package_tool(published="2026-11-01T00:00:00Z")
    tool["lock"] = {"version": "1.0.0",
                    "integrity_sha512": hashlib.sha512(b"pkg-bytes").hexdigest()}
    ok, _ = c.verify_tool_lock(tool, registry_get=_fake_registry_get(packs))
    assert ok
    tool["lock"]["integrity_sha512"] = hashlib.sha512(b"other").hexdigest()
    ok, reason = c.verify_tool_lock(tool, registry_get=_fake_registry_get(packs))
    assert not ok and "rug-pull" in reason


# ---------------------------------------------------------------- scan container args (mocked docker)

def test_scan_container_args_never_mount_real_workspace_or_secrets(tmp_path):
    from poc.kaggle_worker.sandbox import SandboxSettings

    captured: list = []
    settings = SandboxSettings(image="kite-sandbox:latest")
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "package.tgz").write_bytes(b"x")
    tools = c.list_tools_in_container(
        settings, pkg_dir=pkg_dir, pkg_filename="package.tgz", ecosystem="npm",
        server_command=["node", "index.js"],
        runner=_container_runner(captured, tools=[{"name": "t", "description": ""}]))
    assert [t["name"] for t in tools] == ["t"]
    args = captured[0]["args"]
    assert "--rm" in args and "--network" in args and "none" in args
    assert "/pkg:ro" in " ".join(args)
    # The only mounts are the empty throw-away workspace (a /tmp/kite-scan-* dir) and
    # the read-only package dir — never the real workspace, never a secret.
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
    assert any(m.startswith("/tmp/kite-scan-") and m.endswith(":/work") for m in mounts)
    assert not any(m.split(":")[0] == str(tmp_path) for m in mounts)
    script = args[args.index("sh") + 2]
    assert "--ignore-scripts" in script
    assert "package.tgz" in script
    # The stdio transcript the container gets is exactly initialize -> list.
    assert [json.loads(line).get("method") for line in captured[0]["input"].splitlines()] == [
        "initialize", "notifications/initialized", "tools/list"]


def test_scan_tool_persists_report_and_last_scan(tmp_path):
    registry = {"mcp": [_clean_tool(tmp_path)], "skills": [], "platforms": []}
    c.save_registry(tmp_path, registry)
    report = c.scan_tool(tmp_path, "tool-abc123", collection="mcp", now=NOW,
                         mcpiranha_runner=_mcpiranha_runner([]),
                         runner=_stdio_runner([{"name": "add", "description": ""}]))
    assert report.verdict == "pass"
    scans = list((tmp_path / "connect" / "scans").glob("tool-abc123-*.json"))
    assert len(scans) == 1
    assert json.loads(scans[0].read_text())["verdict"] == "pass"
    assert c.latest_scan(tmp_path, "tool-abc123")["verdict"] == "pass"


def test_enable_gate_refuses_unscanned_and_failed_tools():
    tool = {"id": "t", "kind": "source", "last_scan": None}
    ok, reason = c.can_enable(tool)
    assert not ok and "chưa quét" in reason
    tool["last_scan"] = {"verdict": "fail", "findings": []}
    ok, _ = c.can_enable(tool)
    assert not ok
    tool["last_scan"] = {"verdict": "pass", "findings": []}
    ok, _ = c.can_enable(tool)
    assert ok


# ---------------------------------------------------------------- public_tool: the flat display fields the web
# contract promises (F2 fix — the cards read these at the top level).

def test_public_tool_flattens_config_and_derives_status():
    base = {"id": "tool-1", "kind": "source", "name": "s", "enabled": False,
            "config": {"path": "/srv/mcp", "command": ["python3", "server.py", "--x"]},
            "lock": None, "last_scan": None}
    out = c.public_tool(base)
    assert out["command"] == "python3"
    assert out["args"] == ["server.py", "--x"]
    assert out["source"] == "/srv/mcp"
    assert out["path"] == "/srv/mcp"
    assert out["status"] == "pending_scan"
    # The nested config stays intact for the engine; no secret leaks.
    assert out["config"]["command"] == ["python3", "server.py", "--x"]

    out = c.public_tool({**base, "enabled": True,
                         "last_scan": {"verdict": "pass", "findings": []}})
    assert out["status"] == "enabled"

    out = c.public_tool({**base, "last_scan": {"verdict": "pass", "findings": []}})
    assert out["status"] == "pending_approval"

    out = c.public_tool({**base, "last_scan": {"verdict": "fail", "findings": []}})
    assert out["status"] == "disabled"

    remote = {"id": "tool-2", "kind": "remote", "name": "r", "enabled": False,
              "config": {"url": "https://mcp.example.com/mcp", "auth_secret": "tok-SECRET"},
              "lock": None, "last_scan": None}
    out = c.public_tool(remote)
    assert out["url"] == "https://mcp.example.com/mcp"
    assert "tok-SECRET" not in json.dumps(out)

    pkg = {"id": "tool-3", "kind": "package", "name": "p", "enabled": False,
           "config": {"ecosystem": "npm", "package": "some-mcp", "version": "1.0.0"},
           "lock": None, "last_scan": None}
    out = c.public_tool(pkg)
    assert out["package_name"] == "some-mcp"
    assert out["version"] == "1.0.0"
    assert out["status"] == "pending_scan"

    skill = {"id": "tool-4", "kind": "skill", "name": "sk", "enabled": True,
             "config": {"path": "/skills/helper", "scanner": "skillshield+argus"},
             "lock": None, "last_scan": {"verdict": "pass", "findings": []}}
    out = c.public_tool(skill)
    assert out["path"] == "/skills/helper"
    assert out["status"] == "enabled"


# ---------------------------------------------------------------- owner entries: no plaintext auth, gates enforced

def test_owner_entries_only_passing_enabled_tools_and_no_plaintext_token(tmp_path):
    from poc.kaggle_worker.web import security as sec
    from poc.kaggle_worker.web.workers import read_secret, write_secret

    registry = {"mcp": [
        {"id": "tool-good", "kind": "source", "enabled": True,
         "config": {"command": ["python3", "server.py"]},
         "last_scan": {"verdict": "pass", "findings": []}},
        {"id": "tool-unscanned", "kind": "source", "enabled": True,
         "config": {"command": ["python3", "server.py"]}, "last_scan": None},
        {"id": "tool-remote", "kind": "remote", "enabled": True,
         "config": {"url": "https://mcp.example.com/mcp", "auth_secret": "tok-SECRET"},
         "last_scan": {"verdict": "pass", "findings": []}},
        {"id": "tool-pkg", "kind": "package", "enabled": True,
         "config": {"ecosystem": "npm", "package": "some-mcp", "version": "1.0.0"},
         "lock": None, "last_scan": {"verdict": "pass", "findings": []}},
    ], "skills": [], "platforms": []}
    c.save_registry(tmp_path, registry)
    sealer = sec.Sealer(tmp_path / "secrets" / "web_key")
    write_secret(c.secret_path(tmp_path, "tool-remote"), "tok-SECRET", sealer=sealer)
    entries = c.owner_mcp_entries(None, tmp_path)
    assert set(entries) == {"tool-good", "tool-remote"}  # unscanned + lockless package out
    assert entries["tool-good"] == {"type": "local", "command": ["python3", "server.py"],
                                    "enabled": True}
    auth = entries["tool-remote"]["headers"]["Authorization"]
    assert "tok-SECRET" not in auth and "${KITE_CONNECT_MCP_TOOL_REMOTE_TOKEN}" in auth
    env = c.remote_token_env(tmp_path, "tool-remote", sealer)
    assert env == {"KITE_CONNECT_MCP_TOOL_REMOTE_TOKEN": "tok-SECRET"}
    assert "tok-SECRET" not in json.dumps(c.public_tool(registry["mcp"][2]))
    assert read_secret(c.secret_path(tmp_path, "tool-remote"), sealer=sealer) == "tok-SECRET"


# ---------------------------------------------------------------- skill adapter plug (chosen by evaluation:
# skillshield 1.3.0 + Argus 0.2.1 as the pair gate "skillshield+argus")

def test_skill_scan_without_adapter_fails_closed(tmp_path):
    tool = {"id": "tool-skill", "kind": "skill", "enabled": False,
            "config": {"path": str(tmp_path), "scanner": None}, "last_scan": None}
    report = c.scan_tool_config(tool, state_dir=tmp_path, now=NOW)
    assert report.verdict == "fail"
    assert report.findings[0].rule == "skill-scanner-missing"
    with pytest.raises(c.ScannerMissingError):
        c.scan_skill(tmp_path, scanner_name="no-such-scanner")


def test_skill_adapter_plugs_in_and_its_report_is_used(tmp_path):
    class FakeScanner:
        name = "fake-skill-scanner"
        version = "0.1.0"

        def scan(self, path, *, timeout=120):
            return c.ScanReport(verdict="pass", findings=[], scanner=self.name,
                                scanner_version=self.version, scanned_at=c.utcnow_iso(NOW))

    c.register_skill_scanner(FakeScanner())
    try:
        tool = {"id": "tool-skill", "kind": "skill", "enabled": False,
                "config": {"path": str(tmp_path), "scanner": "fake-skill-scanner"},
                "last_scan": None}
        report = c.scan_tool_config(tool, state_dir=tmp_path, now=NOW)
        assert report.verdict == "pass"
        assert report.scanner == "fake-skill-scanner"
    finally:
        c.SKILL_SCANNERS.pop("fake-skill-scanner", None)


# ---------------------------------------------------------------- chat_files merge (minimal, Gói 2 owns per-env)

def test_chat_files_merges_only_passing_enabled_owner_mcp(tmp_path):
    from tests.poc.kaggle_worker.test_mcp_server import make_config
    from poc.kaggle_worker.mcp_server import chat_files

    registry = {"mcp": [
        {"id": "tool-good", "kind": "source", "enabled": True,
         "config": {"command": ["python3", "server.py"]},
         "last_scan": {"verdict": "pass", "findings": []}},
        {"id": "tool-stale", "kind": "source", "enabled": True,
         "config": {"command": ["python3", "server.py"]}, "last_scan": None},
    ], "skills": [], "platforms": []}
    c.save_registry(tmp_path, registry)
    files = chat_files(make_config(tmp_path), lead_model="qwen3.6", kite_command=["kite"],
                       allowed_roots=[tmp_path], state_dir=tmp_path)
    mcp = json.loads(files["opencode.json"])["mcp"]
    assert mcp["kite"]["command"] == ["kite"]  # the built-in entry is untouched
    assert mcp["tool-good"] == {"type": "local", "command": ["python3", "server.py"],
                                "enabled": True}
    assert "tool-stale" not in mcp
    # Without a state_dir nothing merges and nothing breaks.
    files = chat_files(make_config(tmp_path), lead_model="qwen3.6", kite_command=["kite"],
                       allowed_roots=[tmp_path])
    assert set(json.loads(files["opencode.json"])["mcp"]) == {"kite"}
