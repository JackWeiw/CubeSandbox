"""Repro: cube SDK control-plane call hangs forever on a blackholed server.

`requests` defaults to ``timeout=None`` (block forever). ``Config.request_timeout``
(default 30s) already bounds the **data plane** (``_transport.build_client``) and
the **volume API** (``_volume``), but the **sandbox/template control plane** never
wired it — every control-plane call inherits the ``None`` default and a single
blackholed request wedges the SDK indefinitely.

This script spins up a local blackhole TCP server (accepts the connection, never
responds) and calls ``Sandbox.kill()`` (the DELETE path — the exact scenario from
the oversub cleanup hang) against it. A wall-clock cap detects the hang on
**master** without waiting forever; on the **fix branch** the call must raise
within ``Config.request_timeout``.

Usage:
    python repro_control_plane_hang.py [method]    # method in {kill, get_info}; default kill

Run against each branch:
    PYTHONPATH=<clone>/sdk/python python repro_control_plane_hang.py
"""
from __future__ import annotations

import socket
import sys
import threading
import time

import requests
import cubesandbox

SANDBOX_ID = "sb-repro-001"
PORT = 18099
REQ_TIMEOUT = 2.0   # Config.request_timeout (fix-branch honours it; master ignores it)
HANG_CAP = 8.0      # wall-clock cap: if the call is still alive here, it's hanging


def blackhole_server(port: int, stop: threading.Event) -> None:
    """Accept connections and hold them open without ever reading or responding."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)
    srv.settimeout(0.5)
    conns: list[socket.socket] = []
    while not stop.is_set():
        try:
            conn, _ = srv.accept()
            conns.append(conn)  # hold the ref so the kernel keeps the conn open
        except socket.timeout:
            continue
    for c in conns:
        try:
            c.close()
        except OSError:
            pass
    srv.close()


def main() -> None:
    method = sys.argv[1] if len(sys.argv) > 1 else "kill"
    stop = threading.Event()
    threading.Thread(target=blackhole_server, args=(PORT, stop), daemon=True).start()
    time.sleep(0.3)  # let the listener bind

    cfg = cubesandbox.Config(
        api_url=f"http://127.0.0.1:{PORT}", request_timeout=REQ_TIMEOUT
    )
    sb = cubesandbox.Sandbox(
        {
            "sandboxID": SANDBOX_ID,
            "templateID": "tpl",
            "domain": "cube.app",
            "state": "running",
            "cpuCount": 1,
            "memoryMB": 128,
        },
        config=cfg,
    )
    fn = getattr(sb, method)
    result: dict = {}

    def worker() -> None:
        t0 = time.monotonic()
        try:
            fn()
            result["outcome"] = "returned (unexpected — blackhole replied?)"
        except requests.exceptions.Timeout as e:
            result["outcome"] = f"raised {type(e).__name__} (bounded, ok)"
        except Exception as e:  # noqa: BLE001
            result["outcome"] = f"raised {type(e).__name__}: {e}"
        result["elapsed"] = time.monotonic() - t0

    w = threading.Thread(target=worker, daemon=True)
    w.start()
    w.join(HANG_CAP)
    # Capture liveness BEFORE tearing down the blackhole — closing the held
    # conns would RST the blocked read and spuriously "finish" a hanging call.
    alive = w.is_alive()
    stop.set()
    time.sleep(0.3)

    print(f"[repro] SDK: {cubesandbox.__file__}")
    print(f"[repro] Sandbox.{method}() against blackhole 127.0.0.1:{PORT} "
          f"(Config.request_timeout={REQ_TIMEOUT}s, hang-cap={HANG_CAP:.0f}s)")
    if alive:
        print(f"[repro] RESULT: STILL HANGING after {HANG_CAP:.0f}s -- "
              f"requests default timeout=None, Config.request_timeout IGNORED. BUG.")
    else:
        print(f"[repro] RESULT: {result.get('outcome')} after "
              f"{result.get('elapsed', 0):.2f}s -- Config.request_timeout honoured. FIX.")


if __name__ == "__main__":
    main()
