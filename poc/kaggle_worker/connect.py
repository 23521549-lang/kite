"""Connect — the security gate for owner-added MCP servers, skills and platforms (Gói 5).

Every tool the owner adds goes ``add -> scan -> owner reads the report -> enable``; nothing
is enabled without a passing scan, and a package is never enabled without a matching lock.
The five package limits from the plan are enforced in code, not in prose:

1. Never install/run a package on the host. Downloads are plain HTTPS GETs on the host
   (data only, nothing executes) and every install/list step runs in a throw-away scan
   container (``--rm``, ``network=none``, no workspace/secret mount, ``--ignore-scripts``).
2. Only the official registries (``registry.npmjs.org`` / ``pypi.org``), an exactly pinned
   version (no ``latest``, no ``^``/``~``/ranges) and a sha512 integrity recorded in the
   lock and re-checked on every use (enable, and scan).
3. Quarantine: a version published less than 7 days ago scans as ``wait``, never ``pass``.
4. The whole dependency tree is scanned through OSV.dev ``querybatch``; one bad dep fails
   the whole package.
5. At real run time an MCP package still runs natively on the EC2 host with the owner's
   rights — the API answers carry that warning (``runtime_warning``) so the UI shows it,
   and enable is the owner's explicit acceptance of that residual risk.

Network decision for the package download step (Gói 5, option (b)): the tarball is fetched
on the host with a plain HTTPS GET, hash-checked, then copied into the scan container,
which stays offline. The container never touches the Internet (law D3: setup is offline
by default) and the host never executes package code. Option (a) — a bridged scan
container with full Internet — was rejected for that reason.

MCPiranha is the scanner for MCP servers with source code (v0.4.1, verified live 25/11:
``mcpiranha scan <dir> --format json`` reports ``findings`` with ``rule_id``/``severity``/
``location``; a sample that collected ``os.environ`` keys hit ``MCP-SRC-002`` high, while
a sample that exfiltrated a file through ``subprocess`` + ``curl`` scored zero findings —
the AST scan alone does not see generic exfiltration, which is why tools/list in the
container and the OSV sweep exist alongside it). MCPiranha is deliberately NOT a
``pyproject.toml`` dependency: 0.4.1 requires ``mcp>=2.3`` and Kite pins ``mcp<2``
(pip's resolver reports the conflict); it runs as an external binary, and a missing
binary fails the scan closed instead of silently passing.

The skill scanner is a plug-in adapter (:class:`SkillScanner`): the evaluated pair
gate is ``skillshield`` 1.3.0 + Argus 0.2.1 (registered at import, see below);
an unknown scanner name fails closed with :class:`ScannerMissingError`.
Docker is not installed on this VM, so the scan container is built (reusing
``docker_run_args``) and unit-tested with a mocked docker call; a live container
run still needs Docker (CI/EC2).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from typing import Callable, Protocol
import urllib.error
import urllib.parse
import urllib.request

MCPIRANHA_VERSION = "0.4.1"
QUARANTINE_DAYS = 7
QUARANTINE_SECONDS = QUARANTINE_DAYS * 86400
OFFICIAL_NPM_REGISTRY = "https://registry.npmjs.org"
OFFICIAL_PYPI_REGISTRY = "https://pypi.org"
PYPI_FILES_HOST = "files.pythonhosted.org"
# Skill scanners chosen by worker (a2)'s evaluation (25/11, 4 candidates x 4 samples):
# skillshield 1.3.0 is the main gate (4/4); Argus 0.2.1 is the mandatory companion
# (the only candidate that catches plain-language exfiltration, which skillshield
# misses). A skill is enabled only when BOTH are clean. Neither alone suffices.
SKILLSHIELD_VERSION = "1.3.0"
ARGUS_VERSION = "0.2.1"
ARGUS_PACKAGE = "@kurtfrancisco/argus"
DEFAULT_SKILL_SCANNER = "skillshield+argus"
OSV_QUERYBATCH_URL = "https://api.osv.dev/v1/querybatch"
MAX_DEP_NODES = 100
MAX_TARBALL_BYTES = 50 * 1024 * 1024
FAIL_SEVERITIES = {"critical", "high"}
# Tool-poisoning findings fail the scan at any severity: a poisoned description is the
# headline MCP threat, and a "medium" poisoned prompt is still a poisoned prompt.
FAIL_RULE_PREFIXES = ("MCP-TP-",)
RUNTIME_WARNING = ("chủ duyệt = chấp nhận mã này chạy với quyền owner trên EC2; "
                   "quét lại + kiểm lock mỗi lần đổi version; có nút bật/tắt + gỡ")

CONNECT_DIRNAME = "connect"
REGISTRY_FILENAME = "tools.json"
SCANS_DIRNAME = "scans"


class ConnectError(ValueError):
    """A Connect operation cannot proceed (bad input, failed scan step, bad lock)."""


class ScannerMissingError(ConnectError):
    """The scanner a scan needs is not installed or not selected yet."""


class McpClientError(ConnectError):
    """The MCP server did not answer tools/list, or answered something unusable."""


# --------------------------------------------------------------------- reports

@dataclass
class Finding:
    rule: str
    severity: str            # critical | high | medium | low
    message: str
    scanner: str = ""
    location: str = ""

    def to_dict(self) -> dict:
        out = {"rule": self.rule, "severity": self.severity, "message": self.message}
        if self.scanner:
            out["scanner"] = self.scanner
        if self.location:
            out["location"] = self.location
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> "Finding":
        return cls(rule=str(raw.get("rule", "")), severity=str(raw.get("severity", "medium")),
                   message=str(raw.get("message", "")), scanner=str(raw.get("scanner", "")),
                   location=str(raw.get("location", "")))


@dataclass
class ScanReport:
    verdict: str             # pass | fail | wait
    findings: list[Finding] = field(default_factory=list)
    scanner: str = ""
    scanner_version: str = ""
    scanned_at: str = ""
    tools: list[str] = field(default_factory=list)   # tool names tools/list actually saw
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out: dict = {"verdict": self.verdict,
                     "findings": [f.to_dict() for f in self.findings],
                     "scanner": self.scanner, "scanner_version": self.scanner_version,
                     "scanned_at": self.scanned_at}
        if self.tools:
            out["tools"] = list(self.tools)
        out.update(self.extra)
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> "ScanReport":
        return cls(verdict=str(raw.get("verdict", "fail")),
                   findings=[Finding.from_dict(f) for f in raw.get("findings", [])],
                   scanner=str(raw.get("scanner", "")),
                   scanner_version=str(raw.get("scanner_version", "")),
                   scanned_at=str(raw.get("scanned_at", "")),
                   tools=[str(t) for t in raw.get("tools", [])],
                   extra={k: v for k, v in raw.items()
                          if k not in ("verdict", "findings", "scanner", "scanner_version",
                                       "scanned_at", "tools")})


def utcnow_iso(now: float | None = None) -> str:
    moment = datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def decide_verdict(findings: list[Finding], *, quarantined: bool = False) -> str:
    """fail beats wait beats pass. Any high/critical finding fails, as does any
    tool-poisoning finding at any severity."""
    for finding in findings:
        if finding.severity in FAIL_SEVERITIES or finding.rule.startswith(FAIL_RULE_PREFIXES):
            return "fail"
    return "wait" if quarantined else "pass"


def make_report(verdict: str, findings: list[Finding], scanner: str, scanner_version: str,
                *, scanned_at: str | None = None, tools: list[str] | None = None,
                extra: dict | None = None) -> ScanReport:
    return ScanReport(verdict=verdict, findings=list(findings), scanner=scanner,
                      scanner_version=scanner_version,
                      scanned_at=scanned_at or utcnow_iso(), tools=list(tools or []),
                      extra=dict(extra or {}))


# --------------------------------------------------------------------- registry

def connect_dir(state_dir: Path) -> Path:
    return Path(state_dir) / CONNECT_DIRNAME


def registry_path(state_dir: Path) -> Path:
    return connect_dir(state_dir) / REGISTRY_FILENAME


def scans_dir(state_dir: Path) -> Path:
    return connect_dir(state_dir) / SCANS_DIRNAME


def secret_path(state_dir: Path, tool_id: str) -> Path:
    """Where a remote MCP's Authorization token lives (sealed; same convention as the
    per-environment GitHub PATs: ``state_dir/secrets/...``)."""
    return Path(state_dir) / "secrets" / f"connect_mcp_{tool_id}"


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(temp, path)


def empty_registry() -> dict:
    return {"mcp": [], "skills": [], "platforms": []}


def load_registry(state_dir: Path) -> dict:
    """The owner's Connect registry. A corrupt file fails closed (never silently reset:
    silently dropping the registry would silently drop every scan verdict)."""
    path = registry_path(state_dir)
    if not path.exists():
        return empty_registry()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConnectError(f"không đọc được registry Connect {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConnectError(f"registry Connect {path} không phải object JSON")
    registry = empty_registry()
    for key in registry:
        items = raw.get(key, [])
        if not isinstance(items, list):
            raise ConnectError(f"registry Connect {path}: mục {key!r} không phải danh sách")
        registry[key] = items
    return registry


def save_registry(state_dir: Path, registry: dict) -> None:
    _atomic_write_json(registry_path(state_dir), registry)


def new_tool_id() -> str:
    return "tool-" + os.urandom(6).hex()


def find_tool(registry: dict, collection: str, tool_id: str) -> dict | None:
    for tool in registry.get(collection, []):
        if tool.get("id") == tool_id:
            return tool
    return None


def tool_status(tool: dict) -> str:
    """The display status the web contract promises per inventory item
    (HOP-DONG-GIAO-TIEP.md §Connect): enabled / disabled / pending_scan /
    pending_approval. Derived from the stored fields only — no network, no scan
    (the enable gate in ``can_enable`` still re-checks the scan + package lock
    at the moment that matters)."""
    if tool.get("enabled"):
        return "enabled"
    last = tool.get("last_scan") or {}
    if not last:
        return "pending_scan"
    if last.get("verdict") == "pass":
        return "pending_approval"
    return "disabled"


def public_tool(tool: dict) -> dict:
    """What the browser may see: never a secret value, only whether one is stored.

    The flat display fields are part of the web contract (HOP-DONG-GIAO-TIEP.md
    §Connect; the frontend's ``ConnectMcpItem``/``ConnectSkillItem`` types):
    the stored shape keeps everything nested under ``config`` but the cards
    read ``command``/``args``/``source``/``url``/``package_name``/``version``/
    ``path``/``status`` at the top level. Two fields are deliberately NOT
    flattened here: ``tools`` (listing them spawns the MCP server — the owner
    gets them from the on-demand test endpoint instead) and ``lock_mismatch``
    (checking it needs a live registry round-trip per package tool).
    """
    out = {k: v for k, v in tool.items() if k not in ("auth_secret", "token", "secret")}
    config = dict(tool.get("config", {}))
    for key in ("auth_secret", "token", "secret", "authorization"):
        config.pop(key, None)
    out["config"] = config
    out["has_auth"] = bool(tool.get("config", {}).get("auth_secret") or tool.get("has_auth"))
    if tool.get("kind") == "package":
        out["runtime_warning"] = RUNTIME_WARNING
    # Flat display fields (see docstring): the first command element is the
    # executable, the rest are its arguments.
    command = config.get("command")
    if isinstance(command, list) and command:
        out["command"] = str(command[0])
        out["args"] = [str(part) for part in command[1:]]
    out["source"] = config.get("path")
    out["path"] = config.get("path")
    out["url"] = config.get("url")
    out["package_name"] = config.get("package")
    out["version"] = config.get("version")
    out["status"] = tool_status(tool)
    return out


# --------------------------------------------------------------------- package pins, quarantine, locks

_NPM_NAME = re.compile(r"^(?:@[a-z0-9\-~][a-z0-9\-._~]*/)?[a-z0-9\-~][a-z0-9\-._~]*$")
_PYPI_NAME = re.compile(r"^([A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9])$")
_EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def validate_package_spec(ecosystem: str, name: str, version: str, registry: str) -> None:
    """Limit 2, first half: official registry + exactly pinned version. Anything else
    (``latest``, ``^``/``~``, ranges, URLs) is rejected before any network happens."""
    if ecosystem == "npm":
        if registry.rstrip("/") != OFFICIAL_NPM_REGISTRY:
            raise ConnectError(f"chỉ dùng registry chính thức {OFFICIAL_NPM_REGISTRY}")
        if not _NPM_NAME.match(name or ""):
            raise ConnectError(f"tên package npm không hợp lệ: {name!r}")
    elif ecosystem == "pypi":
        if registry.rstrip("/") != OFFICIAL_PYPI_REGISTRY:
            raise ConnectError(f"chỉ dùng registry chính thức {OFFICIAL_PYPI_REGISTRY}")
        if not _PYPI_NAME.match(name or ""):
            raise ConnectError(f"tên package PyPI không hợp lệ: {name!r}")
    else:
        raise ConnectError(f"ecosystem không hỗ trợ: {ecosystem!r} (chỉ npm, pypi)")
    if not _EXACT_VERSION.match(version or ""):
        raise ConnectError(
            f"version phải ghim tuyệt đối (vd 1.2.3), không dùng {version!r} "
            "(không latest, không ^/~, không dải)")


def quarantine_check(published_at: str | None, *, now: float | None = None) -> tuple[bool, str]:
    """Limit 3: a version published less than 7 days ago is ``wait``. Returns
    (quarantined, human-readable reason). An unknown publish date fails closed."""
    if not published_at:
        return True, "không biết ngày publish của version này nên phải chờ (quarantine)"
    try:
        published = parse_iso(published_at)
    except ValueError:
        return True, f"ngày publish không đọc được: {published_at!r}"
    now_dt = datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc)
    age = (now_dt - published).total_seconds()
    if age < QUARANTINE_SECONDS:
        days = age / 86400
        return True, (f"version publish cách đây {days:.1f} ngày (< {QUARANTINE_DAYS} ngày quarantine) — "
                      "chờ đủ 7 ngày rồi quét lại")
    return False, ""


def sha512_hex(data: bytes) -> str:
    return hashlib.sha512(data).hexdigest()


def normalize_integrity(integrity: str) -> str:
    """npm integrity is ``sha512-<base64>``; Kite's lock keeps hex. Both spellings compare."""
    text = (integrity or "").strip()
    if text.startswith("sha512-"):
        try:
            import base64
            return base64.b64decode(text[len("sha512-"):]).hex()
        except ValueError:
            return text
    return text.lower()


def verify_lock_integrity(lock: dict | None, expected_integrity: str) -> bool:
    """Limit 2, second half: the stored lock must match the registry's integrity, checked
    with a constant-time comparison. No lock, or no expected value, never verifies."""
    if not lock or not expected_integrity:
        return False
    stored = normalize_integrity(str(lock.get("integrity_sha512", "")))
    expected = normalize_integrity(expected_integrity)
    return bool(stored and expected) and hmac.compare_digest(stored, expected)


def verify_tarball_bytes(data: bytes, expected_sha512: str) -> bool:
    if not expected_sha512:
        return False
    return hmac.compare_digest(sha512_hex(data), normalize_integrity(expected_sha512))


# --------------------------------------------------------------------- HTTP (host-side only, data only)

def _http_get_json(url: str, *, timeout: int = 30) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "Kite-connect/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ConnectError(f"GET {url} trả {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise ConnectError(f"không GET được {url}: {exc}") from exc


def _http_get_bytes(url: str, *, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Kite-connect/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_TARBALL_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise ConnectError(f"GET {url} trả {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ConnectError(f"không GET được {url}: {exc}") from exc
    if len(data) > MAX_TARBALL_BYTES:
        raise ConnectError(f"tarball {url} lớn hơn giới hạn {MAX_TARBALL_BYTES} bytes")
    return data


def _http_post_json(url: str, payload: dict, *, timeout: int = 30) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "Kite-connect/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ConnectError(f"POST {url} trả {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise ConnectError(f"không POST được {url}: {exc}") from exc


def _check_tarball_host(url: str, allowed_hosts: tuple[str, ...]) -> None:
    host = urllib.parse.urlsplit(url).hostname or ""
    if host not in allowed_hosts:
        raise ConnectError(f"tarball host {host!r} không nằm trong registry chính thức "
                           f"(chỉ {', '.join(allowed_hosts)})")


# --------------------------------------------------------------------- registry metadata + dep tree

def npm_package_metadata(name: str, version: str, *,
                         registry_get: Callable[[str], dict] | None = None) -> dict:
    """``{tarball_url, integrity_sha512, published_at}`` for an exactly pinned npm
    version. The tarball URL must stay on the official registry host (limit 2: no
    arbitrary tarball URLs)."""
    get = registry_get or _http_get_json
    quoted = urllib.parse.quote(name, safe="@/")
    packument = get(f"{OFFICIAL_NPM_REGISTRY}/{quoted}")
    versions = packument.get("versions", {})
    if version not in versions:
        raise ConnectError(f"npm {name}@{version} không có trên {OFFICIAL_NPM_REGISTRY}")
    dist = versions[version].get("dist", {})
    tarball, integrity = dist.get("tarball", ""), dist.get("integrity", "")
    if not tarball or not integrity:
        raise ConnectError(f"npm {name}@{version} thiếu dist.tarball/integrity")
    _check_tarball_host(tarball, ("registry.npmjs.org",))
    published = (packument.get("time", {}) or {}).get(version)
    return {"tarball_url": tarball, "integrity_sha512": integrity, "published_at": published,
            "dependencies": dict(versions[version].get("dependencies", {}) or {})}


def pypi_package_metadata(name: str, version: str, *,
                          registry_get: Callable[[str], dict] | None = None) -> dict:
    """``{file_url, integrity_sha256, published_at}`` for a pinned PyPI version. PyPI
    publishes sha256 (not sha512); the lock stores the sha512 Kite computes from the
    same bytes after the sha256 check, so the lock still verifies on every use."""
    get = registry_get or _http_get_json
    quoted = urllib.parse.quote(name, safe="")
    doc = get(f"{OFFICIAL_PYPI_REGISTRY}/pypi/{quoted}/{urllib.parse.quote(version, safe='')}/json")
    urls = doc.get("urls", []) or []
    if not urls:
        raise ConnectError(f"PyPI {name}=={version} không có file nào")
    # Prefer a wheel (installable without a build step, which would run code); fall back
    # to an sdist only to fail closed later in the container step.
    chosen = next((u for u in urls if u.get("packagetype") == "bdist_wheel"), urls[0])
    digests = chosen.get("digests", {}) or {}
    if not chosen.get("url") or not digests.get("sha256"):
        raise ConnectError(f"PyPI {name}=={version} thiếu url/digest sha256")
    _check_tarball_host(chosen["url"], ("files.pythonhosted.org", "pypi.org"))
    return {"file_url": chosen["url"], "integrity_sha256": digests["sha256"],
            "published_at": chosen.get("upload_time_iso_8601"),
            "packagetype": chosen.get("packagetype"),
            "requires_dist": list((doc.get("info", {}) or {}).get("requires_dist") or [])}


def _parse_version_tuple(text: str) -> tuple[int, int, int] | None:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", text or "")
    if not match:
        return None
    return tuple(int(g) for g in match.groups())  # type: ignore[return-value]


def npm_resolve_range(versions: list[str], spec: str) -> str | None:
    """Resolve an npm range to the highest matching version (exact, ``^``, ``~``,
    ``>=``/``>``/``<=``/``<`` combos, ``1.x``/``*``). Anything unparsable — including
    ``latest`` and dist-tags — resolves to None so the caller fails closed."""
    spec = (spec or "").strip()
    candidates = sorted((v for v in versions if _parse_version_tuple(v)),
                        key=lambda v: _parse_version_tuple(v))  # type: ignore[arg-type]
    if not candidates:
        return None
    if spec in ("", "*", "x", "X"):
        return candidates[-1]
    if _EXACT_VERSION.match(spec):
        return spec if spec in candidates else None
    match = re.match(r"^(\d+)\.(\d+)\.(x|\*)$", spec) or re.match(r"^(\d+)\.(x|\*)$", spec)
    if match:
        major = int(match.group(1))
        minor = int(match.group(2)) if match.group(2) not in ("x", "*") else None
        pool = [v for v in candidates if _parse_version_tuple(v)[0] == major
                and (minor is None or _parse_version_tuple(v)[1] == minor)]
        return pool[-1] if pool else None
    # Space-separated AND of comparators: ^a.b.c, ~a.b.c, >=a.b.c, <a.b.c, ...
    lower: tuple[int, int, int] | None = None
    upper: tuple[int, int, int] | None = None
    lower_inclusive, upper_inclusive = True, True
    for part in spec.split():
        if part.startswith("^"):
            base = _parse_version_tuple(part[1:])
            if base is None:
                return None
            lower, lower_inclusive = base, True
            upper = (base[0] + 1, 0, 0) if base[0] > 0 else (
                (0, base[1] + 1, 0) if base[1] > 0 else (0, 0, base[2] + 1))
            upper_inclusive = False
        elif part.startswith("~"):
            base = _parse_version_tuple(part[1:])
            if base is None:
                return None
            lower, lower_inclusive = base, True
            upper, upper_inclusive = (base[0], base[1] + 1, 0), False
        elif part.startswith(">="):
            base = _parse_version_tuple(part[2:])
            if base is None:
                return None
            lower, lower_inclusive = base, True
        elif part.startswith(">"):
            base = _parse_version_tuple(part[1:])
            if base is None:
                return None
            lower, lower_inclusive = base, False
        elif part.startswith("<="):
            base = _parse_version_tuple(part[2:])
            if base is None:
                return None
            upper, upper_inclusive = base, True
        elif part.startswith("<"):
            base = _parse_version_tuple(part[1:])
            if base is None:
                return None
            upper, upper_inclusive = base, False
        elif _EXACT_VERSION.match(part):
            base = _parse_version_tuple(part)
            lower, upper = base, base
            lower_inclusive, upper_inclusive = True, True
        else:
            return None
    pool = []
    for version in candidates:
        parsed = _parse_version_tuple(version)
        if lower is not None and (parsed < lower or (parsed == lower and not lower_inclusive)):
            continue
        if upper is not None and (parsed > upper or (parsed == upper and not upper_inclusive)):
            continue
        pool.append(version)
    return pool[-1] if pool else None


def resolve_dep_tree_npm(name: str, version: str, *,
                         registry_get: Callable[[str], dict] | None = None,
                         max_nodes: int = MAX_DEP_NODES) -> tuple[list[dict], list[str]]:
    """The exact dependency tree of a pinned npm package (limit 4): every transitive
    dep resolved to an exact version through the registry. Returns (packages,
    unresolved): unresolved ranges fail the scan closed instead of being skipped."""
    get = registry_get or _http_get_json
    resolved: dict[str, str] = {name: version}
    unresolved: list[str] = []
    queue = [(name, version)]
    while queue and len(resolved) <= max_nodes:
        current, current_version = queue.pop(0)
        try:
            meta = npm_package_metadata(current, current_version, registry_get=get)
        except ConnectError as exc:
            unresolved.append(f"{current}@{current_version} ({exc})")
            continue
        for dep, spec in meta["dependencies"].items():
            if dep in resolved:
                continue
            packument = get(f"{OFFICIAL_NPM_REGISTRY}/{urllib.parse.quote(dep, safe='@/')}")
            picked = npm_resolve_range(list((packument.get("versions", {}) or {}).keys()), spec)
            if picked is None:
                unresolved.append(f"{dep} ({spec})")
                continue
            resolved[dep] = picked
            queue.append((dep, picked))
    if len(resolved) > max_nodes:
        unresolved.append(f"cây dep vượt {max_nodes} node — không quét hết được")
    packages = [{"ecosystem": "npm", "name": n, "version": v} for n, v in sorted(resolved.items())]
    return packages, unresolved


def resolve_dep_tree_pypi(name: str, version: str, *,
                          registry_get: Callable[[str], dict] | None = None,
                          max_nodes: int = MAX_DEP_NODES) -> tuple[list[dict], list[str]]:
    """Same as :func:`resolve_dep_tree_npm`, for PyPI (PEP 440 specifiers via the
    ``packaging`` library Kite already has transitively). A requirement without a
    specifier is unresolved — an unpinned dep is never silently skipped."""
    try:
        from packaging.requirements import Requirement
        from packaging.version import Version
    except ImportError:
        return [{"ecosystem": "pypi", "name": name, "version": version}], [
            f"{name}=={version} (thư viện packaging không có — không giải được cây dep)"]
    get = registry_get or _http_get_json
    resolved: dict[str, str] = {name: version}
    unresolved: list[str] = []
    queue = [(name, version)]
    while queue and len(resolved) <= max_nodes:
        current, current_version = queue.pop(0)
        try:
            meta = pypi_package_metadata(current, current_version, registry_get=get)
        except ConnectError as exc:
            unresolved.append(f"{current}=={current_version} ({exc})")
            continue
        for raw_req in meta["requires_dist"]:
            try:
                req = Requirement(raw_req)
            except ValueError:
                unresolved.append(f"{current}=={current_version}: requires_dist lạ {raw_req!r}")
                continue
            if req.marker is not None and not req.marker.evaluate():
                continue
            dep = req.name
            if dep in resolved:
                continue
            if not req.specifier:
                unresolved.append(f"{dep} (không ghim version)")
                continue
            doc = get(f"{OFFICIAL_PYPI_REGISTRY}/pypi/{urllib.parse.quote(dep, safe='')}/json")
            releases = [v for v in (doc.get("releases", {}) or {}).keys()
                        if (doc["releases"][v] or [])]
            matching = sorted((v for v in releases if Version(v) in req.specifier),
                              key=Version)
            if not matching:
                unresolved.append(f"{dep} ({req.specifier})")
                continue
            resolved[dep] = matching[-1]
            queue.append((dep, matching[-1]))
    if len(resolved) > max_nodes:
        unresolved.append(f"cây dep vượt {max_nodes} node — không quét hết được")
    packages = [{"ecosystem": "pypi", "name": n, "version": v} for n, v in sorted(resolved.items())]
    return packages, unresolved


# --------------------------------------------------------------------- OSV.dev

def osv_querybatch(packages: list[dict], *,
                   http_post: Callable[[str, dict], dict] | None = None) -> list[dict]:
    """Ask OSV.dev about a whole dependency tree in one call (no key needed). Returns
    one entry per package: ``{ecosystem, name, version, vulns: [{id, summary}]}``."""
    post = http_post or (lambda url, payload: _http_post_json(url, payload))
    queries = [{"package": {"name": p["name"], "ecosystem": p["ecosystem"]},
                "version": p["version"]} for p in packages]
    data = post(OSV_QUERYBATCH_URL, {"queries": queries})
    results = data.get("results", [])
    out = []
    for package, result in zip(packages, results):
        vulns = []
        for vuln in (result or {}).get("vulns", []) or []:
            vulns.append({"id": str(vuln.get("id", "")),
                          "summary": str(vuln.get("summary", "") or vuln.get("details", "")[:200])})
        out.append({"ecosystem": package["ecosystem"], "name": package["name"],
                    "version": package["version"], "vulns": vulns})
    return out


def osv_findings(packages: list[dict], *,
                 http_post: Callable[[str, dict], dict] | None = None) -> list[Finding]:
    """Limit 4: one bad dep fails the whole package. A ``MAL-*`` record (a package
    OSV knows to be malicious) is critical; any other advisory is high."""
    findings = []
    for entry in osv_querybatch(packages, http_post=http_post):
        for vuln in entry["vulns"]:
            severity = "critical" if vuln["id"].startswith("MAL-") else "high"
            findings.append(Finding(
                rule=f"OSV-{vuln['id']}", severity=severity,
                message=(f"{entry['ecosystem']}/{entry['name']}@{entry['version']}: "
                         f"{vuln['id']}" + (f" — {vuln['summary']}" if vuln["summary"] else "")),
                scanner="osv.dev"))
    return findings


# --------------------------------------------------------------------- MCP client: tools/list (JSON-RPC over stdio)

def mcp_initialize_messages() -> list[dict]:
    return [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "kite-connect", "version": "1.0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]


def _tools_from_message(message) -> list[dict] | None:
    """The tools list from one decoded JSON-RPC message, or None when the
    message is not a tools/list answer. An empty list is a real answer."""
    result = message.get("result") if isinstance(message, dict) else None
    if isinstance(result, dict) and isinstance(result.get("tools"), list):
        tools = []
        for tool in result["tools"]:
            if isinstance(tool, dict) and tool.get("name"):
                tools.append({"name": str(tool["name"]),
                              "description": str(tool.get("description", "")),
                              "inputSchema": tool.get("inputSchema", {})})
        return tools
    return None


def parse_tools_list_output(text: str) -> list[dict]:
    """Tool names from a tools/list transcript. Servers may print log lines to stdout;
    only JSON lines with a ``result.tools`` list count — anything else is ignored, and
    no tools list at all is an error, not an empty list."""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        tools = _tools_from_message(message)
        if tools is not None:
            return tools
    raise McpClientError("server không trả tools/list (không thấy result.tools)")


def _default_stdio_runner(command: list[str], input_text: str, timeout: int) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(command, input=input_text, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise McpClientError(f"server không trả lời trong {timeout}s") from exc
    except OSError as exc:
        raise McpClientError(f"không chạy được server {command[0]!r}: {exc}") from exc
    return proc.returncode, proc.stdout, proc.stderr


def _list_tools_stdio_live(command: list[str], timeout: int) -> list[dict]:
    """Drive a real MCP server interactively: write the handshake, then read
    until the tools/list answer arrives — stdin stays OPEN while waiting.

    Closing stdin right after writing (what ``subprocess.run(input=...)`` does)
    tells the server to shut down; an async server (FastMCP) can cancel the
    pending tools/list answer before it is written, so the scan flakily fails
    with 'không thấy result.tools' (measured live 25/11: 3/8 under load, and
    the failing transcript held only the initialize answer although stderr
    showed the tools/list request had been processed)."""
    import select

    try:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
    except OSError as exc:
        raise McpClientError(f"không chạy được server {command[0]!r}: {exc}") from exc
    tools = None
    try:
        assert proc.stdin is not None and proc.stdout is not None
        try:
            for message in mcp_initialize_messages():
                proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            proc.stdin.flush()
        except BrokenPipeError as exc:
            # The server exited before reading the handshake (e.g. `echo`):
            # its stdin is gone, so the write has nowhere to go. A dead
            # server is a failed tools/list, not a scan crash — the caller
            # turns McpClientError into a `tools-list-failed` finding instead
            # of the endpoint returning HTTP 500 (measured 26/11: the race
            # fired ~2/3 of parallel scans of an exiting command).
            raise McpClientError(
                "server thoát trước khi nhận handshake (broken pipe)") from exc
        # NOTE: stdin stays open here on purpose (see docstring).
        deadline = time.monotonic() + timeout
        buf = b""
        out_fd = proc.stdout.fileno()
        while tools is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                raise McpClientError(f"server không trả lời trong {timeout}s")
            ready, _, _ = select.select([out_fd], [], [], remaining)
            if not ready:
                proc.kill()
                raise McpClientError(f"server không trả lời trong {timeout}s")
            chunk = os.read(out_fd, 65536)
            if not chunk:
                break  # EOF without a tools/list answer
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("{"):
                    continue
                try:
                    message = json.loads(text)
                except ValueError:
                    continue
                tools = _tools_from_message(message)
                if tools is not None:
                    break
    finally:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    if tools is not None:
        return tools
    # EOF without an answer: the server died or never spoke MCP — say why.
    if proc.returncode not in (0, None):
        err = proc.stderr.read() if proc.stderr is not None else ""
        raise McpClientError(f"server thoát mã {proc.returncode}: {err.strip()[-500:]}")
    raise McpClientError("server không trả tools/list (không thấy result.tools)")


def list_tools_stdio(command: list[str], *, timeout: int = 30,
                     runner: Callable[[list[str], str, int], tuple[int, str, str]] | None = None
                     ) -> list[dict]:
    """Run an MCP server on stdio and ask what tools it exposes (initialize ->
    notifications/initialized -> tools/list). This is Kite's first MCP client: the repo
    had none (grep tools/list/jsonrpc = 0 before Gói 5). A mocked ``runner`` takes the
    whole transcript at once (tests); the real path drives the server interactively
    (see :func:`_list_tools_stdio_live`)."""
    if not command or not all(isinstance(part, str) and part for part in command):
        raise McpClientError("lệnh chạy MCP server trống hoặc không phải chuỗi")
    if runner is not None:
        payload = "\n".join(json.dumps(m) for m in mcp_initialize_messages()) + "\n"
        returncode, stdout, stderr = runner(list(command), payload, timeout)
        if returncode != 0:
            raise McpClientError(f"server thoát mã {returncode}: {stderr.strip()[-500:]}")
        return parse_tools_list_output(stdout)
    return _list_tools_stdio_live(list(command), timeout)


def list_tools_remote(url: str, headers: dict | None = None, *, timeout: int = 30,
                      http_post: Callable | None = None) -> list[dict]:
    """tools/list over Streamable HTTP: POST initialize, keep the Mcp-Session-Id the
    server answers with, POST tools/list. A server that does not speak MCP over HTTP
    fails the probe — fail closed, not 'assume fine'."""
    import urllib.request as _request

    def default_post(target: str, payload: dict, post_headers: dict):
        req = _request.Request(target, data=json.dumps(payload).encode("utf-8"),
                               headers={"Content-Type": "application/json",
                                        "Accept": "application/json, text/event-stream",
                                        **post_headers})
        try:
            with _request.urlopen(req, timeout=timeout) as response:
                return response.status, dict(response.headers), response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError) as exc:
            raise McpClientError(f"không gọi được remote MCP {target}: {exc}") from exc

    post = http_post or default_post
    base_headers = dict(headers or {})
    status, resp_headers, body = post(url, mcp_initialize_messages()[0], base_headers)
    if status >= 400:
        raise McpClientError(f"remote MCP initialize trả HTTP {status}")
    session_id = resp_headers.get("Mcp-Session-Id") or resp_headers.get("mcp-session-id")
    if session_id:
        base_headers["Mcp-Session-Id"] = session_id
    status, _headers, body = post(url, mcp_initialize_messages()[2], base_headers)
    if status >= 400:
        raise McpClientError(f"remote MCP tools/list trả HTTP {status}")
    # A Streamable HTTP answer may be SSE (data: {...} lines) or plain JSON.
    text = body
    if "data:" in body:
        text = "\n".join(line[5:].strip() for line in body.splitlines()
                         if line.strip().startswith("data:"))
    return parse_tools_list_output(text)


# --------------------------------------------------------------------- MCPiranha (source scan)

def mcpiranha_available() -> bool:
    return shutil.which("mcpiranha") is not None


def _default_mcpiranha_runner(command: list[str], timeout: int) -> tuple[int, str, str]:
    if shutil.which("mcpiranha") is None:
        raise ScannerMissingError(
            "không tìm thấy binary mcpiranha — cài mcpiranha 0.4.1 (ngoài pyproject: "
            "nó đòi mcp>=2.3, xung đột với mcp<2 của Kite) rồi quét lại")
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ConnectError(f"mcpiranha không xong trong {timeout}s") from exc
    except OSError as exc:
        raise ConnectError(f"không chạy được mcpiranha: {exc}") from exc
    return proc.returncode, proc.stdout, proc.stderr


def run_mcpiranha(path: Path, *,
                  runner: Callable[[list[str], int], tuple[int, str, str]] | None = None,
                  timeout: int = 180) -> tuple[list[Finding], str]:
    """Scan MCP source with MCPiranha (``scan <path> --format json`` — verified live
    25/11). Returns (findings, scanner_version). A finding's rule is the MCPiranha
    rule id (vd MCP-SRC-002), so the report names the exact rule that fired."""
    run = runner or _default_mcpiranha_runner
    returncode, stdout, stderr = run(["mcpiranha", "scan", str(path), "--format", "json"], timeout)
    try:
        data = json.loads(stdout)
    except ValueError:
        if returncode != 0:
            raise ConnectError(f"mcpiranha thoát mã {returncode}: {stderr.strip()[-500:]}")
        raise ConnectError("mcpiranha trả output không phải JSON")
    findings = []
    for raw in data.get("findings", []) or []:
        location = raw.get("location", {}) or {}
        where = ""
        if location.get("file"):
            where = str(location["file"])
            if location.get("line"):
                where += f":{location['line']}"
        evidence = raw.get("evidence", []) or []
        message = str(raw.get("title", raw.get("rule_id", "")))
        if raw.get("rationale"):
            message += f" — {raw['rationale']}"
        elif evidence:
            message += f" — {evidence[0].get('text', '')}"
        findings.append(Finding(rule=str(raw.get("rule_id", "MCPIRANHA")),
                                severity=str(raw.get("severity", "medium")).lower(),
                                message=message, scanner="mcpiranha", location=where))
    return findings, str(data.get("scanner_version", MCPIRANHA_VERSION))


# --------------------------------------------------------------------- skill scanner adapter (worker (a) plugs in here)

class SkillScanner(Protocol):
    """Adapter for a skill scanner chosen by worker (a)'s evaluation. The adapter
    receives (command, pinned version) from that evaluation; it scans a skill's files
    and returns a ScanReport in the same shape as every other scan."""
    name: str
    version: str

    def scan(self, path: Path, *, timeout: int = 120) -> ScanReport: ...


SKILL_SCANNERS: dict[str, SkillScanner] = {}


def register_skill_scanner(scanner: SkillScanner) -> None:
    SKILL_SCANNERS[scanner.name] = scanner


def skill_scanner_names() -> list[str]:
    return sorted(SKILL_SCANNERS)


def get_skill_scanner(name: str) -> SkillScanner:
    scanner = SKILL_SCANNERS.get(name)
    if scanner is None:
        raise ScannerMissingError(
            f"chưa có skill scanner tên {name!r} — worker (a) đang đánh giá 4 ứng viên "
            "(agent-skill-scanner, Argus, skillshield, cisco-ai-skill-scanner); "
            "skill vẫn bị khóa cho tới khi một adapter được đăng ký")
    return scanner


def scan_skill(path: Path, *, scanner_name: str, timeout: int = 120) -> ScanReport:
    scanner = get_skill_scanner(scanner_name)
    report = scanner.scan(Path(path), timeout=timeout)
    if not isinstance(report, ScanReport):
        raise ConnectError(f"skill scanner {scanner_name!r} trả báo cáo không đúng dạng")
    return report


# --------------------------------------------------------------------- skill scanner adapters (chosen by evaluation)
#
# Gate rules (from worker (a2)'s report, measured 25/11 on 4 samples):
# - skillshield: block unless every result has risk_level "SAFE" AND "unscanned"
#   is empty. Exit 0 = clean, 1 = flagged; any other code is an error.
# - Argus: block unless exit 0 (its own gate is failOn "high"). Its JSON carries
#   findings with ruleId/severity/message/target.
# - The pair gate (DEFAULT_SKILL_SCANNER) passes only when BOTH are clean.
# Both adapters invoke external binaries and fail closed when the binary is
# missing (ScannerMissingError) — the scan never silently passes.

RunnerFn = Callable[[list[str], int], tuple[int, str, str]]


def _default_cli_runner(command: list[str], timeout: int) -> tuple[int, str, str]:
    """Run an external scanner binary. Missing binary -> ScannerMissingError (fail
    closed); timeout / OS error -> ConnectError. Exit-code meaning is the caller's."""
    if shutil.which(command[0]) is None:
        raise ScannerMissingError(f"không tìm thấy binary {command[0]!r} — chưa cài scanner")
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ConnectError(f"scanner {command[0]!r} không xong trong {timeout}s") from exc
    except OSError as exc:
        raise ConnectError(f"không chạy được scanner {command[0]!r}: {exc}") from exc
    return proc.returncode, proc.stdout, proc.stderr


def _parse_json_output(stdout: str, *, scanner: str) -> dict:
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise ConnectError(f"{scanner} trả output không phải JSON") from exc
    if not isinstance(data, dict):
        raise ConnectError(f"{scanner} trả JSON không phải object")
    return data


class SkillShieldScanner:
    """skillshield (PyPI, MIT, zero runtime deps) — the main skill gate."""

    name = "skillshield"
    version = SKILLSHIELD_VERSION

    def __init__(self, runner: RunnerFn | None = None) -> None:
        self._runner = runner or _default_cli_runner

    def scan(self, path: Path, *, timeout: int = 120) -> ScanReport:
        returncode, stdout, stderr = self._runner(
            ["skillshield", "scan", str(path), "--format", "json"], timeout)
        if returncode not in (0, 1):
            raise ConnectError(f"skillshield thoát mã {returncode}: {stderr.strip()[-500:]}")
        data = _parse_json_output(stdout, scanner="skillshield")
        findings: list[Finding] = []
        failed = False
        for result in data.get("results", []) or []:
            if str(result.get("risk_level", "")).upper() != "SAFE":
                failed = True
            for raw in result.get("findings", []) or []:
                name = str(raw.get("name", "")).strip()
                desc = str(raw.get("description", "")).strip()
                message = f"{name}: {desc}"[:500] if name else desc[:500]
                findings.append(Finding(
                    rule=str(raw.get("id", "skillshield")),
                    severity=str(raw.get("severity", "medium")).lower(),
                    message=message, scanner="skillshield",
                    location=str(result.get("skill", ""))))
        unscanned = data.get("unscanned", []) or []
        if unscanned:
            failed = True
            findings.append(Finding(
                rule="unscanned-files", severity="high",
                message=(f"skillshield bỏ qua {len(unscanned)} mục chưa quét: "
                         f"{', '.join(str(u) for u in unscanned[:5])}"),
                scanner="skillshield"))
        if any(f.severity in FAIL_SEVERITIES for f in findings):
            failed = True
        return make_report("fail" if failed else "pass", findings,
                           "skillshield", SKILLSHIELD_VERSION, scanned_at=utcnow_iso())


class ArgusScanner:
    """Argus of MCP (npm @kurtfrancisco/argus, MIT, zero deps) — the mandatory
    companion: the only evaluated candidate that catches plain-language
    exfiltration, and the only one with a tools-description scan (-t)."""

    name = "argus"
    version = ARGUS_VERSION

    def __init__(self, runner: RunnerFn | None = None) -> None:
        self._runner = runner or _default_cli_runner

    def _command(self, mode_args: list[str]) -> list[str]:
        # Prefer a preinstalled `argus` binary (the deployed image preinstalls it);
        # otherwise fall back to npx with the pinned package (downloads on first use).
        if shutil.which("argus") is not None:
            return ["argus", *mode_args]
        return ["npx", "--yes", f"{ARGUS_PACKAGE}@{ARGUS_VERSION}", *mode_args]

    def _parse_findings(self, stdout: str) -> tuple[list[Finding], str]:
        data = _parse_json_output(stdout, scanner="argus")
        findings: list[Finding] = []
        for raw in data.get("findings", []) or []:
            findings.append(Finding(
                rule=str(raw.get("ruleId", "argus")),
                severity=str(raw.get("severity", "medium")).lower(),
                message=str(raw.get("message", "") or raw.get("title", ""))[:500],
                scanner="argus", location=str(raw.get("target", ""))))
        version = str((data.get("tool") or {}).get("version", ARGUS_VERSION))
        return findings, version

    def _decide(self, findings: list[Finding], returncode: int) -> tuple[list[Finding], bool]:
        """Argus's own gate is its exit code (failOn "high"): exit 1 means it
        flagged something. If it tripped but no single finding is critical/high,
        keep the trip visible with a summary finding so the scan still fails."""
        failed = returncode != 0 or any(f.severity in FAIL_SEVERITIES for f in findings)
        if failed and not any(f.severity in FAIL_SEVERITIES for f in findings):
            findings = list(findings) + [Finding(
                rule="argus-flagged", severity="high",
                message=(f"argus gắn cờ {len(findings)} finding "
                         "(dưới ngưỡng critical/high riêng lẻ nhưng vượt ngưỡng "
                         "chặn của chính argus)"),
                scanner="argus")]
        return findings, failed

    def scan(self, path: Path, *, timeout: int = 120) -> ScanReport:
        """Scan a skill directory (-s mode)."""
        returncode, stdout, stderr = self._runner(
            self._command(["-s", str(path), "--no-default-roots", "-f", "json",
                           "--no-color"]), timeout)
        if returncode not in (0, 1):
            raise ConnectError(f"argus thoát mã {returncode}: {stderr.strip()[-500:]}")
        findings, version = self._parse_findings(stdout)
        findings, failed = self._decide(findings, returncode)
        return make_report("fail" if failed else "pass", findings,
                           "argus", version, scanned_at=utcnow_iso())

    def scan_tools(self, tools: list[dict], *, timeout: int = 120
                   ) -> tuple[list[Finding], str, bool]:
        """Scan MCP tool descriptions (-t mode) from a tools/list dump.
        Returns (findings, version, failed). The dump shape is {"tools": [...]}
        with each tool's name/description/inputSchema, exactly what
        parse_tools_list_output produces."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
            json.dump({"tools": tools}, tmp)
            tmp_path = tmp.name
        try:
            returncode, stdout, stderr = self._runner(
                self._command(["-t", tmp_path, "--no-default-roots", "-f", "json",
                               "--no-color"]), timeout)
        finally:
            os.unlink(tmp_path)
        if returncode not in (0, 1):
            raise ConnectError(f"argus thoát mã {returncode}: {stderr.strip()[-500:]}")
        findings, version = self._parse_findings(stdout)
        findings, failed = self._decide(findings, returncode)
        return findings, version, failed


class SkillPairScanner:
    """The pair gate: skillshield AND Argus must both be clean. A skill is
    enabled only when this composite passes."""

    name = "skillshield+argus"
    version = f"{SKILLSHIELD_VERSION}+{ARGUS_VERSION}"

    def __init__(self, shield: SkillShieldScanner | None = None,
                 argus: ArgusScanner | None = None) -> None:
        self._shield = shield or SkillShieldScanner()
        self._argus = argus or ArgusScanner()

    def scan(self, path: Path, *, timeout: int = 120) -> ScanReport:
        shield_report = self._shield.scan(Path(path), timeout=timeout)
        argus_report = self._argus.scan(Path(path), timeout=timeout)
        findings = list(shield_report.findings) + list(argus_report.findings)
        failed = shield_report.verdict != "pass" or argus_report.verdict != "pass"
        return make_report("fail" if failed else "pass", findings,
                           "skillshield+argus", self.version, scanned_at=utcnow_iso())


register_skill_scanner(SkillShieldScanner())
register_skill_scanner(ArgusScanner())
register_skill_scanner(SkillPairScanner())


def _argus_tools_findings(tools: list[dict], *, runner: RunnerFn | None = None
                          ) -> tuple[list[Finding], bool]:
    """Argus -t over a tools/list dump. Returns (findings, ran). Fail closed:
    a missing binary or a CLI error becomes a high finding, never a silent pass.
    Empty tool list -> nothing to scan (no findings, ran=False)."""
    if not tools:
        return [], False
    argus = ArgusScanner(runner=runner)
    try:
        findings, _version, _failed = argus.scan_tools(tools)
    except ScannerMissingError as exc:
        return [Finding(rule="scanner-missing", severity="high", message=str(exc),
                        scanner="argus")], True
    except ConnectError as exc:
        return [Finding(rule="argus-error", severity="high", message=str(exc),
                        scanner="argus")], True
    return findings, True


# --------------------------------------------------------------------- package download (host: HTTPS GET only, never executes)

def download_package_file(url: str, *, expected_sha256: str | None = None,
                          expected_sha512: str | None = None,
                          http_get: Callable[[str], bytes] | None = None) -> bytes:
    """Fetch a package file on the host (network decision (b)). The host only moves
    bytes and checks hashes — it never extracts or executes them. The sha256 (PyPI)
    or sha512 (npm) check happens here, before the bytes go anywhere near a container."""
    get = http_get or _http_get_bytes
    data = get(url)
    if expected_sha256:
        if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected_sha256.lower()):
            raise ConnectError("sha256 của file tải về không khớp metadata registry — dừng lại")
    if expected_sha512 and not verify_tarball_bytes(data, expected_sha512):
        raise ConnectError("sha512 của file tải về không khớp metadata registry — dừng lại")
    return data


# --------------------------------------------------------------------- scan container (Docker; mocked in tests here)

@contextmanager
def scan_container_args(settings, *, name: str, pkg_dir: Path):
    """``docker run`` args for the throw-away scan container, reusing
    ``docker_run_args`` from ``sandbox.py`` (``--rm``, caps dropped, no-new-privileges,
    memory/pids limits, ``network=none``). The workspace ``docker_run_args`` always
    mounts is a fresh EMPTY temp dir — never the real workspace, never a secret — and
    the package dir is mounted read-only at ``/pkg``. No live run happens on this VM
    (no Docker here); the args are unit-tested with a mocked runner and the live run
    is a remaining gate for CI/EC2."""
    from .sandbox import docker_run_args

    pkg_dir = Path(pkg_dir).resolve()
    with tempfile.TemporaryDirectory(prefix="kite-scan-") as empty:
        args = docker_run_args(settings, name=name, workspace=Path(empty), network="none")
        args[-1:-1] = ["-v", f"{pkg_dir}:/pkg:ro"]
        yield args


def container_scan_script(ecosystem: str, pkg_filename: str, server_command: list[str]) -> str:
    """What runs inside the scan container: install the verified package offline with
    scripts disabled, then exec the MCP server so its stdio can be listed. npm's
    ``--ignore-scripts`` blocks preinstall/install/postinstall AND the node-gyp
    rebuild path (verified live 25/11 on a sample with a postinstall marker +
    ``binding.gyp``: no marker, no ``build/`` dir with the flag; the marker appeared
    without it). It does NOT block code that runs at require/import time — that code
    runs inside this throw-away container, never on the host, and at real run time it
    runs natively on EC2, which is the accepted residual risk (limit 5)."""
    if ecosystem == "npm":
        install = (f"mkdir -p /tmp/scan && cd /tmp/scan && npm install --ignore-scripts "
                   f"--no-audit --no-fund /pkg/{pkg_filename}")
    elif ecosystem == "pypi":
        if not pkg_filename.endswith(".whl"):
            raise ConnectError("PyPI sdist cần bước build (chạy code) — chỉ nhận wheel; "
                               "package này bị chặn")
        install = (f"mkdir -p /tmp/scan && python3 -m pip install --no-deps --quiet "
                   f"--target /tmp/scan /pkg/{pkg_filename}")
    else:
        raise ConnectError(f"ecosystem không hỗ trợ: {ecosystem!r}")
    quoted = " ".join(_shell_quote(part) for part in server_command)
    return f"{install} && exec {quoted}"


def _shell_quote(part: str) -> str:
    import shlex
    return shlex.quote(part)


def _default_container_runner(args: list[str], input_text: str, timeout: int
                              ) -> tuple[int, str, str]:
    if shutil.which("docker") is None:
        raise ConnectError("không có docker trên máy này — kiểm chứng container quét live "
                           "cần Docker (CI/EC2)")
    try:
        proc = subprocess.run(args, input=input_text, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise McpClientError(f"scan container không trả lời trong {timeout}s") from exc
    except OSError as exc:
        raise ConnectError(f"không chạy được docker: {exc}") from exc
    return proc.returncode, proc.stdout, proc.stderr


def list_tools_in_container(settings, *, pkg_dir: Path, pkg_filename: str,
                            ecosystem: str, server_command: list[str], timeout: int = 120,
                            runner: Callable[[list[str], str, int], tuple[int, str, str]] | None = None
                            ) -> list[dict]:
    """Install the verified package offline in the scan container and list the tools
    its MCP server exposes, over the container's stdio. The server's code runs only
    inside the throw-away container."""
    run = runner or _default_container_runner
    script = container_scan_script(ecosystem, pkg_filename, server_command)
    payload = "\n".join(json.dumps(m) for m in mcp_initialize_messages()) + "\n"
    with scan_container_args(settings, name=f"kite-scan-{os.urandom(4).hex()}",
                             pkg_dir=pkg_dir) as args:
        # docker_run_args puts the image last; the command goes between the flags and it.
        full = args[:-1] + ["sh", "-c", script, args[-1]]
        returncode, stdout, stderr = run(full, payload, timeout)
    if returncode != 0:
        raise McpClientError(f"scan container thoát mã {returncode}: {stderr.strip()[-500:]}")
    return parse_tools_list_output(stdout)


# --------------------------------------------------------------------- scan orchestration

def _finding(rule: str, severity: str, message: str, scanner: str = "") -> Finding:
    return Finding(rule=rule, severity=severity, message=message, scanner=scanner)


def scan_tool_config(tool: dict, *, state_dir: Path, now: float | None = None,
                     http_post: Callable[[str, dict], dict] | None = None,
                     http_get: Callable[[str], bytes] | None = None,
                     registry_get: Callable[[str], dict] | None = None,
                     runner: Callable[[list[str], str, int], tuple[int, str, str]] | None = None,
                     mcpiranha_runner: Callable[[list[str], int], tuple[int, str, str]] | None = None,
                     container_runner: Callable[[list[str], str, int], tuple[int, str, str]] | None = None,
                     argus_runner: RunnerFn | None = None,
                     remote_headers: dict | None = None, settings=None) -> ScanReport:
    """Scan one registry entry and return its report (does not save it; see
    :func:`scan_tool`). Dispatches by kind; every branch fails closed."""
    kind = tool.get("kind")
    config = tool.get("config", {}) or {}
    scanned_at = utcnow_iso(now)
    if kind in ("local", "source"):
        return _scan_source(tool, config, scanned_at, mcpiranha_runner=mcpiranha_runner,
                            runner=runner, argus_runner=argus_runner)
    if kind == "package":
        return _scan_package(tool, config, state_dir, scanned_at, now=now, http_post=http_post,
                             http_get=http_get, registry_get=registry_get,
                             container_runner=container_runner, settings=settings,
                             argus_runner=argus_runner)
    if kind == "remote":
        return _scan_remote(tool, config, scanned_at, remote_headers=remote_headers,
                            argus_runner=argus_runner)
    if kind == "skill":
        return _scan_skill_entry(tool, config, scanned_at)
    raise ConnectError(f"kind không hỗ trợ: {kind!r}")


def _scan_source(tool: dict, config: dict, scanned_at: str, *,
                 mcpiranha_runner=None, runner=None, argus_runner=None) -> ScanReport:
    findings: list[Finding] = []
    tools: list[str] = []
    listed: list[dict] = []
    path = config.get("path")
    if path:
        source = Path(path)
        if not source.exists():
            findings.append(_finding("source-missing", "high",
                                     f"mã nguồn MCP không còn ở {path!r}", "kite-connect"))
        else:
            try:
                piranha_findings, version = run_mcpiranha(source,
                                                          runner=mcpiranha_runner)
                findings.extend(piranha_findings)
                scanner_version = version
            except ScannerMissingError as exc:
                findings.append(_finding("scanner-missing", "high", str(exc), "kite-connect"))
                scanner_version = MCPIRANHA_VERSION
    else:
        findings.append(_finding("source-missing", "high",
                                 "MCP local không khai path mã nguồn — không quét được",
                                 "kite-connect"))
        scanner_version = MCPIRANHA_VERSION
    command = config.get("command")
    if command:
        try:
            listed = list_tools_stdio(list(command), runner=runner)
            tools = [t["name"] for t in listed]
        except McpClientError as exc:
            findings.append(_finding("tools-list-failed", "high",
                                     f"không liệt kê được tool của server: {exc}", "kite-connect"))
    label = "mcpiranha+tools-list"
    if listed:
        # Argus -t over the tools/list dump: the only evaluated layer that
        # catches poisoned tool descriptions (MCPiranha's AST reads code, not
        # descriptions). Fail closed when the binary is missing.
        argus_findings, _ran = _argus_tools_findings(listed, runner=argus_runner)
        findings.extend(argus_findings)
        label += "+argus"
    verdict = decide_verdict(findings)
    return make_report(verdict, findings, label, scanner_version,
                       scanned_at=scanned_at, tools=tools)


def _scan_package(tool: dict, config: dict, state_dir: Path, scanned_at: str, *,
                  now=None, http_post=None, http_get=None, registry_get=None,
                  container_runner=None, settings=None, argus_runner=None) -> ScanReport:
    from .sandbox import SandboxSettings

    findings: list[Finding] = []
    tools: list[str] = []
    listed: list[dict] = []
    ecosystem = config.get("ecosystem", "npm")
    name, version = config.get("package", ""), config.get("version", "")
    registry = config.get("registry", OFFICIAL_NPM_REGISTRY if ecosystem == "npm"
                          else OFFICIAL_PYPI_REGISTRY)
    try:
        validate_package_spec(ecosystem, name, version, registry)
    except ConnectError as exc:
        return make_report("fail", [_finding("bad-package-spec", "high", str(exc), "kite-connect")],
                           "kite-connect", "1.0", scanned_at=scanned_at)
    get = registry_get or _http_get_json
    if ecosystem == "npm":
        meta = npm_package_metadata(name, version, registry_get=get)
        published_at, integrity = meta["published_at"], meta["integrity_sha512"]
        file_url, sha256 = meta["tarball_url"], None
        filename = "package.tgz"
        dep_packages, unresolved = resolve_dep_tree_npm(name, version, registry_get=get)
    else:
        meta = pypi_package_metadata(name, version, registry_get=get)
        published_at, integrity = meta["published_at"], None
        file_url, sha256 = meta["file_url"], meta["integrity_sha256"]
        filename = "package.whl" if meta.get("packagetype") == "bdist_wheel" else "package.tar.gz"
        dep_packages, unresolved = resolve_dep_tree_pypi(name, version, registry_get=get)
    quarantined, reason = quarantine_check(published_at, now=now)
    if quarantined:
        findings.append(_finding("quarantine", "medium", reason, "kite-connect"))
    for item in unresolved:
        findings.append(_finding("unresolved-dep", "high",
                                 f"không giải được dep trong cây: {item}", "kite-connect"))
    findings.extend(osv_findings(dep_packages, http_post=http_post))
    # Download on the host (bytes only) and hash-check before anything else touches it.
    try:
        data = download_package_file(file_url, expected_sha256=sha256,
                                     expected_sha512=integrity, http_get=http_get)
    except ConnectError as exc:
        findings.append(_finding("lock-hash-mismatch", "high", str(exc), "kite-connect"))
        verdict = decide_verdict(findings, quarantined=quarantined)
        return make_report(verdict, findings, "osv.dev+lock", "1.0", scanned_at=scanned_at)
    lock: dict = {"version": version, "integrity_sha512": sha512_hex(data),
                  "published_at": published_at, "registry": registry}
    if sha256:
        lock["integrity_sha256"] = sha256
    tool["lock"] = lock
    # List the tools inside the throw-away container, offline, scripts disabled.
    server_command = config.get("server_command") or default_server_command(
        ecosystem, name, version)
    try:
        with tempfile.TemporaryDirectory(prefix="kite-pkg-") as pkg_dir:
            (Path(pkg_dir) / filename).write_bytes(data)
            listed = list_tools_in_container(
                settings or SandboxSettings(), pkg_dir=Path(pkg_dir), pkg_filename=filename,
                ecosystem=ecosystem, server_command=server_command,
                runner=container_runner)
            tools = [t["name"] for t in listed]
    except (McpClientError, ConnectError) as exc:
        findings.append(_finding("container-tools-list-failed", "high",
                                 f"không liệt kê được tool trong container quét: {exc}",
                                 "kite-connect"))
    label = "osv.dev+mcpiranha-container"
    if listed:
        argus_findings, _ran = _argus_tools_findings(listed, runner=argus_runner)
        findings.extend(argus_findings)
        label += "+argus"
    verdict = decide_verdict(findings, quarantined=quarantined)
    return make_report(verdict, findings, label, "1.0",
                       scanned_at=scanned_at, tools=tools,
                       extra={"runtime_warning": RUNTIME_WARNING})


def default_server_command(ecosystem: str, name: str, version: str) -> list[str]:
    if ecosystem == "npm":
        return ["npx", "-y", f"{name}@{version}"]
    return ["uvx", f"{name}=={version}"]


def _scan_remote(tool: dict, config: dict, scanned_at: str, *,
                 remote_headers: dict | None = None, argus_runner=None) -> ScanReport:
    findings: list[Finding] = []
    tools: list[str] = []
    listed: list[dict] = []
    url = config.get("url", "")
    if not url.startswith("https://"):
        findings.append(_finding("remote-not-https", "high",
                                 "remote MCP phải dùng https — http bị chặn", "kite-connect"))
    if config.get("auth_secret") or config.get("token") or config.get("authorization"):
        findings.append(_finding("plaintext-secret", "high",
                                 "secret của remote MCP nằm plaintext trong config — phải lưu "
                                 "niêm phong qua write_secret", "kite-connect"))
    if not findings:
        try:
            listed = list_tools_remote(url, remote_headers)
            tools = [t["name"] for t in listed]
        except McpClientError as exc:
            findings.append(_finding("remote-probe-failed", "high",
                                     f"không probe được remote MCP: {exc}", "kite-connect"))
    label = "kite-connect-remote-probe"
    if listed:
        argus_findings, _ran = _argus_tools_findings(listed, runner=argus_runner)
        findings.extend(argus_findings)
        label += "+argus"
    verdict = decide_verdict(findings)
    return make_report(verdict, findings, label, "1.0",
                       scanned_at=scanned_at, tools=tools)


def _scan_skill_entry(tool: dict, config: dict, scanned_at: str) -> ScanReport:
    path = config.get("path")
    scanner_name = config.get("scanner")
    if not path or not Path(path).exists():
        return make_report("fail", [_finding("skill-missing", "high",
                                             "skill không có path hoặc path không còn tồn tại",
                                             "kite-connect")],
                           "kite-connect", "1.0", scanned_at=scanned_at)
    if not scanner_name:
        return make_report("fail", [_finding("skill-scanner-missing", "high",
                                             "chưa chọn skill scanner — worker (a) đang đánh giá; "
                                             "skill vẫn bị khóa", "kite-connect")],
                           "kite-connect", "1.0", scanned_at=scanned_at)
    try:
        report = scan_skill(Path(path), scanner_name=scanner_name)
    except ScannerMissingError as exc:
        return make_report("fail", [_finding("skill-scanner-missing", "high", str(exc),
                                             "kite-connect")],
                           "kite-connect", "1.0", scanned_at=scanned_at)
    report.scanned_at = scanned_at
    return report


def scan_tool(state_dir: Path, tool_id: str, *, collection: str = "mcp",
              now: float | None = None, **kwargs) -> ScanReport:
    """Scan one tool and persist the report: ``scans/<id>-<ts>.json`` + the tool's
    ``last_scan`` in the registry. The report on disk is the audit trail the owner
    reads before enabling."""
    registry = load_registry(state_dir)
    tool = find_tool(registry, collection, tool_id)
    if tool is None:
        raise ConnectError(f"không có tool {tool_id!r} trong {collection}")
    report = scan_tool_config(tool, state_dir=state_dir, now=now, **kwargs)
    stamp = utcnow_iso(now).replace(":", "").replace("-", "").replace("T", "-")
    scans = scans_dir(state_dir)
    scans.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(scans / f"{tool_id}-{stamp}.json", report.to_dict())
    tool["last_scan"] = report.to_dict()
    tool["updated_at"] = utcnow_iso(now)
    save_registry(state_dir, registry)
    return report


def latest_scan(state_dir: Path, tool_id: str, *, collection: str = "mcp") -> dict | None:
    registry = load_registry(state_dir)
    tool = find_tool(registry, collection, tool_id)
    if tool is None:
        raise ConnectError(f"không có tool {tool_id!r} trong {collection}")
    last = tool.get("last_scan")
    return dict(last) if isinstance(last, dict) else None


def verify_tool_lock(tool: dict, *, registry_get: Callable[[str], dict] | None = None
                     ) -> tuple[bool, str]:
    """Re-check a package tool's lock against the live registry (limit 2: checked on
    every use — enable, and scan). Non-package tools have no lock to check."""
    if tool.get("kind") != "package":
        return True, "không phải package — không có lock"
    config, lock = tool.get("config", {}) or {}, tool.get("lock") or {}
    if not lock:
        return False, "package chưa có lock (chưa quét lần nào)"
    ecosystem = config.get("ecosystem", "npm")
    name, version = config.get("package", ""), config.get("version", "")
    get = registry_get or _http_get_json
    try:
        if ecosystem == "npm":
            meta = npm_package_metadata(name, version, registry_get=get)
            ok = verify_lock_integrity(lock, meta["integrity_sha512"])
            return ok, ("lock khớp registry" if ok else
                        "lock KHÔNG khớp registry — package có thể đã bị thay (rug-pull)")
        meta = pypi_package_metadata(name, version, registry_get=get)
        stored = str(lock.get("integrity_sha256", "")).lower()
        ok = bool(stored) and hmac.compare_digest(stored, meta["integrity_sha256"].lower())
        return ok, ("lock khớp registry" if ok else
                    "lock KHÔNG khớp registry — package có thể đã bị thay (rug-pull)")
    except ConnectError as exc:
        return False, f"không kiểm được lock với registry: {exc}"


def can_enable(tool: dict, *, registry_get: Callable[[str], dict] | None = None
               ) -> tuple[bool, str]:
    """The enable gate: last scan must be ``pass``, and a package lock must verify.
    Anything else is refused with the reason, so the UI can show why."""
    last = tool.get("last_scan") or {}
    if last.get("verdict") != "pass":
        return False, (f"chưa quét đạt (verdict hiện tại: {last.get('verdict', 'chưa quét')}) — "
                       "quét lại rồi mới bật")
    if tool.get("kind") == "package":
        ok, reason = verify_tool_lock(tool, registry_get=registry_get)
        if not ok:
            return False, reason
    return True, ""


# --------------------------------------------------------------------- merge into chat (owner_mcp_entries)

def remote_token_env_name(tool_id: str) -> str:
    safe = re.sub(r"[^A-Z0-9]", "_", str(tool_id).upper())
    return f"KITE_CONNECT_MCP_{safe}_TOKEN"


def remote_token_env(state_dir: Path, tool_id: str, sealer) -> dict:
    """The Authorization token for a remote MCP, read from its sealed file, as an
    environment mapping the chat launcher exports before starting OpenCode. The token
    never appears in opencode.json — the merged entry carries a ``${VAR}`` reference."""
    from .web.workers import read_secret

    if sealer is None:
        raise ConnectError("cần khóa web (sealer) để đọc token remote MCP đã niêm phong")
    return {remote_token_env_name(tool_id): read_secret(secret_path(state_dir, tool_id),
                                                       sealer=sealer)}


def owner_mcp_entries(env_id: str | None, state_dir: Path, *,
                      sealer=None, registry_get: Callable[[str], dict] | None = None) -> dict:
    """Enabled, scan-passing MCP entries for ``chat_files`` to merge into opencode.json.
    ``env_id`` is the per-environment key Gói 2 owns; until Gói 2 lands, ``None`` means
    the global registry (every enabled entry). A remote entry's Authorization header
    is a ``${KITE_CONNECT_MCP_<ID>_TOKEN}`` reference — never the plaintext token.
    Entries that fail any gate are skipped (chat setup must not break because one tool
    went stale); the enable/scan flow is where the owner sees the reason."""
    try:
        registry = load_registry(state_dir)
    except ConnectError:
        return {}
    entries: dict = {}
    for tool in registry.get("mcp", []):
        if not tool.get("enabled"):
            continue
        if (tool.get("last_scan") or {}).get("verdict") != "pass":
            continue
        config = tool.get("config", {}) or {}
        kind = tool.get("kind")
        if kind in ("local", "source"):
            command = config.get("command")
            if not command or not all(isinstance(p, str) and p for p in command):
                continue
            entries[tool["id"]] = {"type": "local", "command": list(command), "enabled": True}
        elif kind == "package":
            if registry_get is not None:
                ok, _reason = verify_tool_lock(tool, registry_get=registry_get)
                if not ok:
                    continue
            elif not tool.get("lock"):
                continue
            command = default_server_command(config.get("ecosystem", "npm"),
                                              config.get("package", ""),
                                              config.get("version", ""))
            entries[tool["id"]] = {"type": "local", "command": command, "enabled": True}
        elif kind == "remote":
            url = config.get("url", "")
            if not url.startswith("https://"):
                continue
            headers = {"Authorization": "Bearer ${" + remote_token_env_name(tool["id"]) + "}"}
            entries[tool["id"]] = {"type": "remote", "url": url, "headers": headers,
                                   "enabled": True}
    return entries
