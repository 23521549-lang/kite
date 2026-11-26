"""Tier-2 egress proxy for bench runs that declare network hosts (Gói 7, bench-standard §5).

The agent container **stays** on the internal sandbox network (``--internal``): it never
gets a route to the Internet. A small proxy container joins that network (as
``egress-proxy``) *and* the host's default bridge (where the Internet is), and forwards
only the hosts the owner approved for this run. The agent container is told the proxy
through ``HTTP_PROXY``/``HTTPS_PROXY`` (plumbing: ``sandbox.proxy_env``); everything
else it tries to reach fails, because the internal network has no route out.

Lifecycle (one proxy per bench run, started by the bench driver, stopped with it):

* ``start_egress_proxy`` writes the proxy script + allowlist under
  ``<state_dir>/bench-egress/<run_id>/``, starts the proxy container from the sandbox
  image (it is ``python:3.12-slim`` based, so ``python3`` is there; no new image to
  pull), then connects it to the ``bridge`` network.
* ``EgressProxy.stop`` removes the container (best effort). The proxy's own log
  (``<run_dir>/egress.log``, one ``ALLOW``/``DENY`` line per request) is the egress
  log the run's journal record points at.

The proxy itself is stdlib-only Python (``PROXY_SCRIPT``): it speaks HTTP ``CONNECT``
(for https) and absolute-form HTTP, checks the target hostname against the allowlist
(exact match, case-insensitive, no wildcard, no bare IP -- the same rule as
``bench_standard.validate_network_hosts``) and refuses anything else with 403 without
opening the upstream connection.

Deliberately *not* tier 1 (putting the agent container on ``bridge``): that would give
the agent the whole Internet, and ``settings.network`` may never carry ``"bridge"``
(validation forbids it, ``ensure_network`` refuses a non-internal network).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Callable

PROXY_PORT = 3128
PROXY_ALIAS = "egress-proxy"
BRIDGE_NETWORK = "bridge"
_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class EgressProxyError(RuntimeError):
    """The proxy container could not be started (Docker missing, bad allowlist, ...)."""


def is_allowed_host(host: str | None, allowlist) -> bool:
    """Exact allowlist match: case-insensitive, a ``:port`` suffix is stripped, a subdomain
    of an allowed host is *not* allowed (no wildcard in v1)."""
    if not isinstance(host, str) or not host.strip():
        return False
    host = host.strip().lower()
    if ":" in host:
        host = host.rsplit(":", 1)[0]
    return host in {str(h).strip().lower() for h in allowlist}


# The proxy server, run as ``python3 /proxy/proxy.py`` inside the proxy container.
# Kept as a string: the container has no Kite code, only the sandbox image's python3.
PROXY_SCRIPT = '''"""Allowlist egress proxy (stdlib only).

Usage: python3 proxy.py --port 3128 --allowlist /proxy/log/allowlist.json --log /proxy/log/egress.log

CONNECT (https) and absolute-form HTTP are forwarded only when the target hostname is
in the allowlist (exact match, case-insensitive); anything else gets 403 and is logged
as DENY, without any upstream connection. Every request is logged, allowed or not.
"""

import argparse
import json
import socket
import threading
import time


def log_line(path, line):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\\n")
        handle.flush()


def load_allowlist(path):
    with open(path, encoding="utf-8") as handle:
        return {str(h).strip().lower() for h in json.load(handle)}


def allowed(host, allowlist):
    host = (host or "").strip().lower()
    if ":" in host:
        host = host.rsplit(":", 1)[0]
    return host in allowlist


def _pump(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _read_head(conn):
    request = b""
    while b"\\r\\n\\r\\n" not in request and len(request) < 65536:
        chunk = conn.recv(4096)
        if not chunk:
            return None, b""
        request += chunk
    head, _, rest = request.partition(b"\\r\\n\\r\\n")
    return head.decode("iso-8859-1"), rest


def handle(conn, allowlist, log_path):
    try:
        head, rest = _read_head(conn)
        if head is None:
            return
        lines = head.split("\\r\\n")
        method, target, _version = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        if method.upper() == "CONNECT":
            hostport = target
            host = hostport.rsplit(":", 1)[0] if ":" in hostport else hostport
            port = int(hostport.rsplit(":", 1)[1]) if ":" in hostport else 443
            if not allowed(host, allowlist):
                log_line(log_path, f"DENY {host}")
                conn.sendall(b"HTTP/1.1 403 Forbidden\\r\\nContent-Length: 0\\r\\n\\r\\n")
                return
            try:
                upstream = socket.create_connection((host, port), timeout=30)
            except OSError:
                conn.sendall(b"HTTP/1.1 502 Bad Gateway\\r\\nContent-Length: 0\\r\\n\\r\\n")
                return
            log_line(log_path, f"ALLOW {host}")
            conn.sendall(b"HTTP/1.1 200 Connection Established\\r\\n\\r\\n")
            if rest:
                # Bytes already read past the CONNECT headers (a client that pipelined
                # its first request) belong to the tunnel, not to the proxy.
                upstream.sendall(rest)
            first = threading.Thread(target=_pump, args=(conn, upstream), daemon=True)
            second = threading.Thread(target=_pump, args=(upstream, conn), daemon=True)
            first.start()
            second.start()
            first.join()
            second.join()
            upstream.close()
            return
        # Plain HTTP: the proxy request line carries the absolute URI.
        from urllib.parse import urlsplit

        split = urlsplit(target)
        host = split.hostname or ""
        port = split.port or (443 if split.scheme == "https" else 80)
        if not allowed(host, allowlist):
            log_line(log_path, f"DENY {host}")
            conn.sendall(b"HTTP/1.1 403 Forbidden\\r\\nContent-Length: 0\\r\\nConnection: close\\r\\n\\r\\n")
            return
        path = split.path or "/"
        if split.query:
            path += "?" + split.query
        out = [f"{method} {path} HTTP/1.1", f"Host: {split.netloc}"]
        for key, value in headers.items():
            if key in ("proxy-connection", "proxy-authorization", "connection"):
                continue
            out.append(f"{key}: {value}")
        out.append("Connection: close")
        try:
            upstream = socket.create_connection((host, port), timeout=30)
        except OSError:
            conn.sendall(b"HTTP/1.1 502 Bad Gateway\\r\\nContent-Length: 0\\r\\nConnection: close\\r\\n\\r\\n")
            return
        log_line(log_path, f"ALLOW {host}")
        upstream.sendall(("\\r\\n".join(out) + "\\r\\n\\r\\n").encode("iso-8859-1") + rest)
        _pump(upstream, conn)
        upstream.close()
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=3128)
    parser.add_argument("--allowlist", required=True)
    parser.add_argument("--log", required=True)
    args = parser.parse_args()
    allowlist = load_allowlist(args.allowlist)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", args.port))
    server.listen(64)
    while True:
        conn, _addr = server.accept()
        threading.Thread(target=handle, args=(conn, allowlist, args.log), daemon=True).start()


if __name__ == "__main__":
    main()
'''


@dataclass
class EgressProxy:
    """A running proxy container for one bench run."""

    name: str
    proxy_url: str                      # http://egress-proxy:3128 (inside the internal network)
    hosts: tuple[str, ...]
    run_dir: Path
    docker_command: tuple[str, ...] = ("docker",)
    runner: Callable[..., subprocess.CompletedProcess] | None = None

    @property
    def log_path(self) -> Path:
        return self.run_dir / "egress.log"

    def stop(self) -> None:
        """Remove the proxy container; best effort (a run that already ended must not fail)."""
        runner = self.runner or _default_runner()
        try:
            runner([*self.docker_command, "rm", "-f", self.name], timeout=60)
        except Exception:
            pass


def _default_runner():
    from .sandbox import default_runner

    return default_runner


def start_egress_proxy(*, state_dir: Path, settings, hosts, run_id: str,
                       runner: Callable[..., subprocess.CompletedProcess] | None = None) -> EgressProxy:
    """Start the proxy container for one bench run and return it (call ``stop()`` with the run).

    ``settings`` is a ``SandboxSettings`` (its internal network + image are reused; the proxy
    never touches ``settings.network`` itself). ``hosts`` is the owner-approved allowlist.
    """
    from . import bench_standard

    errors = bench_standard.validate_network_hosts(list(hosts))
    if errors:
        raise EgressProxyError("; ".join(e.message for e in errors))
    run_id = re.sub(r"[^a-z0-9-]+", "-", run_id.strip().lower()).strip("-") or "bench"
    if not _RUN_ID.match(run_id):
        raise EgressProxyError(f"run_id không hợp lệ: {run_id!r}")
    runner = runner or _default_runner()
    docker = list(settings.docker_command)
    run_dir = Path(state_dir) / "bench-egress" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "proxy.py").write_text(PROXY_SCRIPT, encoding="utf-8")
    (run_dir / "allowlist.json").write_text(
        json.dumps(sorted({h.strip().lower() for h in hosts}), ensure_ascii=False, indent=2),
        encoding="utf-8")
    (run_dir / "egress.log").write_text("", encoding="utf-8")
    name = f"kite-egress-{run_id}"
    # The proxy container starts on the internal network only; it is connected to the
    # bridge (Internet) in the next step, so it is never reachable from the agent side
    # except as the proxy, and never gets a route the agent could use directly.
    started = runner([*docker, "run", "-d", "--rm", "--name", name,
                      "--network", settings.network, "--network-alias", PROXY_ALIAS,
                      "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                      "-v", f"{(run_dir / 'proxy.py').resolve()}:/proxy/proxy.py:ro",
                      "-v", f"{run_dir.resolve()}:/proxy/log",
                      settings.image, "python3", "/proxy/proxy.py",
                      "--port", str(PROXY_PORT),
                      "--allowlist", "/proxy/log/allowlist.json",
                      "--log", "/proxy/log/egress.log"], timeout=120)
    if started.returncode != 0:
        raise EgressProxyError(
            f"không khởi động được egress proxy: {(started.stderr or started.stdout or '').strip()[:300]}")
    connected = runner([*docker, "network", "connect", BRIDGE_NETWORK, name], timeout=60)
    if connected.returncode != 0:
        runner([*docker, "rm", "-f", name], timeout=60)
        raise EgressProxyError(
            f"không nối egress proxy vào mạng bridge: {(connected.stderr or connected.stdout or '').strip()[:300]}")
    return EgressProxy(name=name, proxy_url=f"http://{PROXY_ALIAS}:{PROXY_PORT}",
                       hosts=tuple(sorted({h.strip().lower() for h in hosts})),
                       run_dir=run_dir, docker_command=tuple(docker), runner=runner)
