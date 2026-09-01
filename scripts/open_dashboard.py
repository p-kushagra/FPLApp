"""Wait for the Streamlit server to become healthy, then open the browser.

This lives in Python rather than in `launch_fpl.bat` because the batch version
needed a `powershell -Command` with nested quotes inside a caret-continued
line, and the quoting broke silently: the health probe always reported failure,
the launcher fell through to its timeout branch and sat on a `pause` forever.
A lingering hidden console is exactly what the one-click launcher exists to
avoid, so the fragile part is expressed somewhere it can be tested.

Exit codes: 0 ready (browser opened), 1 timed out, 2 interrupted.
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
import webbrowser

DEFAULT_PORT = 8501
DEFAULT_TIMEOUT = 90
POLL_SECONDS = 1.0

# Streamlit's own liveness endpoint. Cheaper and more honest than fetching the
# page, which returns a shell before the script has finished its first run.
HEALTH_PATH = "/_stcore/health"


def is_healthy(port: int, timeout: float = 2.0) -> bool:
    url = f"http://localhost:{port}{HEALTH_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_for(port: int, timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_healthy(port):
            return True
        time.sleep(POLL_SECONDS)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="seconds to wait for the server")
    parser.add_argument("--no-browser", action="store_true",
                        help="wait for readiness but do not open a browser")
    args = parser.parse_args(argv)

    url = f"http://localhost:{args.port}"
    print(f"      waiting for {url} ...", flush=True)

    try:
        ready = wait_for(args.port, args.timeout)
    except KeyboardInterrupt:
        return 2

    if not ready:
        print(f"      not ready after {args.timeout}s. The dashboard window "
              f"will show the error.", file=sys.stderr)
        return 1

    print("      ready.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
