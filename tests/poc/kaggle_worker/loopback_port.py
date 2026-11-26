"""A free loopback TCP port for the Temporal test server.

Kept in its own module (not in conftest.py) on purpose: the test tree
already contains a second conftest.py
(tests/poc/kaggle_worker/fixtures/conftest.py), and a plain
``from conftest import ...`` resolves to whichever conftest pytest
imported first during full-suite collection, so it is not a reliable
import path. This module name is unique in the collection tree.
"""

from __future__ import annotations

import socket


def free_loopback_port() -> int:
    """A free TCP port on 127.0.0.1 for the Temporal test server.

    temporalio's test server picks its own port with sdk-core's
    get_free_port("0.0.0.0"), which self-connects to 0.0.0.0 — that
    loopback self-connect is intercepted by this VM's egress proxy, so
    start_time_skipping() hangs forever before spawning the server.
    Passing an explicit port skips get_free_port entirely, and binding
    on 127.0.0.1 is not intercepted.
    """
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()
