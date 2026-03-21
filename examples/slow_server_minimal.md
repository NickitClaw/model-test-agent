# Minimal Slow-Start Example

Server:

```bash
python3 examples/slow_start_http_server.py --host 127.0.0.1 --port 18081 --startup-delay 30
```

Client:

```bash
curl --fail --silent http://127.0.0.1:18081/healthz
```
