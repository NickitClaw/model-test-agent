from __future__ import annotations

import signal
import sys
import time


running = True


def stop_handler(signum: int, frame: object) -> None:
    del signum, frame
    print("SERVER STOPPED", flush=True)
    raise SystemExit(0)


signal.signal(signal.SIGINT, stop_handler)
signal.signal(signal.SIGTERM, stop_handler)

print("Mock server booting", flush=True)
time.sleep(0.3)
print("SERVER READY", flush=True)

index = 0
while running:
    print(f"heartbeat {index}", flush=True)
    index += 1
    time.sleep(0.2)
