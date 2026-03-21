# Slow-Start Server Validation Runbook

This runbook validates that the orchestration runtime can wait for a slow server startup before issuing client traffic.

## Important Constraint

The server in this scenario is intentionally slow. It is configured with `--startup-delay 30`, which means it must spend at least **30 seconds** starting before it binds the port and serves requests.

Do **not** run the `curl` command early. If you send the request before the server prints the readiness line, the request can fail with connection errors because the service is not up yet.

Only continue after the server terminal prints:

```text
SERVER READY host=127.0.0.1 port=18081 delay=30.0
```

## Step 1: Launch The Server

Open a terminal and run:

```bash
python3 examples/slow_start_http_server.py --host 127.0.0.1 --port 18081 --startup-delay 30
```

While the process is warming up, it will print lines like:

```text
STARTUP WAIT remaining=29.0s
```

Keep waiting until you see the exact `SERVER READY ...` line shown above.

## Step 2: Call The Service With curl

After the server is ready, open a second terminal and run:

```bash
curl --fail --silent http://127.0.0.1:18081/healthz
```

Expected output:

```json
{"status": "ok", "startup_delay_s": 30.0, "ready_at": 1700000000.0}
```

The `status` field must be `ok`.

## Step 3: Stop The Server

Return to the server terminal and stop it with `Ctrl-C`.
