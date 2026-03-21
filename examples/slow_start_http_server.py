from __future__ import annotations

import argparse
import json
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HTTP server with a configurable minimum startup delay")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--startup-delay", type=float, default=30.0, help="Minimum startup time in seconds")
    return parser.parse_args()


class HealthHandler(BaseHTTPRequestHandler):
    server_version = "SlowStartHTTP/0.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/healthz":
            self.send_error(404, "Not Found")
            return
        payload = {
            "status": "ok",
            "startup_delay_s": self.server.startup_delay_s,
            "ready_at": self.server.ready_at,
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        message = format % args
        print(f"ACCESS {self.address_string()} {message}", flush=True)


def wait_for_minimum_startup(delay_s: float, stop_event: threading.Event) -> bool:
    target = time.monotonic() + delay_s
    while True:
        remaining = target - time.monotonic()
        if remaining <= 0:
            return True
        if stop_event.is_set():
            return False
        print(f"STARTUP WAIT remaining={remaining:.1f}s", flush=True)
        stop_event.wait(timeout=min(1.0, remaining))


def main() -> int:
    args = parse_args()
    stop_event = threading.Event()
    httpd: ThreadingHTTPServer | None = None

    def handle_signal(signum: int, frame: object) -> None:
        del frame
        print(f"SIGNAL {signum} received, shutting down", flush=True)
        stop_event.set()
        if httpd is not None:
            threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print(
        f"SERVER BOOT host={args.host} port={args.port} startup_delay={args.startup_delay:.1f}",
        flush=True,
    )
    ready = wait_for_minimum_startup(args.startup_delay, stop_event)
    if not ready:
        print("SERVER STOPPED before readiness", flush=True)
        return 0

    class SlowStartHTTPServer(ThreadingHTTPServer):
        startup_delay_s = args.startup_delay
        ready_at = time.time()

    httpd = SlowStartHTTPServer((args.host, args.port), HealthHandler)
    try:
        print(
            f"SERVER READY host={args.host} port={args.port} delay={args.startup_delay:.1f}",
            flush=True,
        )
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()
        print("SERVER STOPPED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
