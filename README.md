# Model Test Agent

`model-test-agent` is a Python project for executing AI model runbooks from Markdown or PDF documents on Linux-oriented command-line environments.

It is designed for workflows such as:

- Launching model servers in long-running shells or Docker containers
- Running clients or benchmark commands in parallel sessions
- Connecting to remote Linux hosts with SSH
- Driving interactive programs through persistent terminal sessions, including `vim`, `docker run -it`, and nested shells
- Waiting on live terminal output before advancing to the next action
- Using an OpenAI-compatible API for document planning and agentic execution

## Architecture

The project is intentionally split into four layers:

1. `document_loader.py`
   Reads `.md`, `.txt`, and `.pdf` sources into raw text.
2. `planner.py`
   Uses an OpenAI-compatible model to convert a runbook document into a structured workflow JSON file.
   A deterministic enrichment pass then fills common omitted details such as readiness waits, probes, and cleanup steps.
3. `runtime/`
   Maintains persistent shell sessions through a pluggable backend. `tmux` is supported, and a native PTY backend is included for hosts where `tmux` is unavailable.
4. `agent.py`
   Runs a tool-calling supervisor loop over the workflow, with access to step execution, ad hoc shell actions, output capture, waiting, and diagnostics.

The design is inspired by the background-task pattern in `s08_background_tasks.py`, but extends it to a production-oriented workflow runner:

- Background commands can publish readiness events into a notification queue
- Workflow steps can block, run in the background, or synchronize with barriers
- Session state persists across commands, which is required for shells, `vim`, SSH sessions, and Docker TTYs

## Requirements

- Python 3.9+
- `ssh` installed if remote hosts are used
- `docker` installed if Docker-backed sessions are used
- An OpenAI-compatible API endpoint for planning and agentic execution

`tmux` is optional. By default the runtime selects `tmux` if available and falls back to a native PTY backend otherwise.

PDF ingestion requires:

```bash
pip install "model-test-agent[pdf]"
```

## Environment

The agent reads these environment variables:

- `MTA_BASE_URL` or `OPENAI_BASE_URL`
- `MTA_API_KEY` or `OPENAI_API_KEY`
- `MTA_MODEL` or `OPENAI_MODEL`
- `MTA_PLANNER_MODEL` to override the planning model
- `MTA_AGENT_MODEL` to override the execution model
- `MTA_SESSION_BACKEND=auto|tmux|pty`
- `MTA_TMUX_BIN` to override the `tmux` binary path
- `MTA_LOG_ROOT` to override the per-run log root directory. Defaults to `.mta-runs` under the current working directory.
- `MTA_SESSION_BUFFER_MAX_CHARS` to control how much recent PTY output is retained
- `MTA_CAPTURE_LINES` to control how many recent lines are used for waits, probes, and captures
- `MTA_TIMEOUT_S` to override the default per-step timeout in seconds
- `MTA_POLL_INTERVAL_S` to control backend wait polling cadence
- `MTA_PLANNER_MAX_ATTEMPTS` to control how many times the planner retries when the first JSON does not validate
- `MTA_MAX_ITERATIONS` to control the supervising agent loop limit
- `MTA_STREAM_AGENT_OUTPUT=true|false` to control whether the agent streams its natural-language narration token by token when the model endpoint supports SSE

## CLI

Plan a workflow from a Markdown/PDF document:

```bash
mta plan docs/benchmark.md --output workflow.json
```

Run a generated workflow through the agent:

```bash
mta run-workflow workflow.json
```

When the configured OpenAI-compatible endpoint supports streaming chat completions, the agent narrates its next move incrementally in the terminal, similar to an interactive coding assistant. If the endpoint does not support SSE streaming, the runner falls back to buffered assistant messages automatically.

Run a workflow directly without the LLM agent:

```bash
MTA_SESSION_BACKEND=pty mta exec-workflow workflow.json
```

Each execution creates a per-run log directory and prints a final summary with the log path plus per-session `session.log`, `stdout.log`, and `stderr.log` locations.

Plan and run in one command:

```bash
mta run-document docs/benchmark.md --plan-output workflow.json
```

Check local runtime prerequisites:

```bash
mta doctor
```

List recent structured runs:

```bash
mta runs --limit 10
```

Inspect one run by run id, run directory name, or path:

```bash
mta show-run 13876932
```

Print the formal workflow JSON Schema:

```bash
mta schema --output workflow.schema.json
```

Validate a workflow JSON file before running it:

```bash
mta validate-workflow workflow.json
```

Run the local PTY end-to-end validation:

```bash
cd /Users/nickit/Desktop/workspace/model-test-agent
PYTHONPATH=src MTA_SESSION_BACKEND=pty python3 -m model_test_agent.cli exec-workflow examples/local_mock_workflow.json
```

## Workflow Model

Workflows are stored as JSON. Each workflow defines:

- `sessions`: named shells bound to `local`, `ssh`, `docker_exec`, or `docker_run`
- `steps`: executable units with dependencies

Supported step kinds:

- `command`
- `wait`
- `probe`
- `send_keys`
- `sleep`
- `barrier`
- `capture`
- `decision`

`command` and `wait` steps can run in the background. When a background watcher sees a readiness pattern or a failure pattern, it pushes a notification into the agent loop.

The enrichment layer can infer common missing details from minimal docs, for example:

- Extracting host, port, and URL data from commands when the prose omits them
- Adding a readiness `probe` when a server launch is followed by `curl` or another client step
- Converting a foreground server launch into a background step when later steps need the service to stay up
- Appending a cleanup `Ctrl-C` step when the runbook omits server shutdown

The enricher now also prefers endpoint-aware matching when multiple services appear in the same runbook, which reduces false positives where one later `curl` could otherwise be attached to the wrong launch step.

The project now exposes a formal JSON Schema through `mta schema`. Internally, planned and loaded workflows are also validated for:

- Duplicate step ids
- Unknown session references
- Unknown dependencies in `depends_on`
- Unknown `wait_for` targets in barrier steps
- Unknown `target_step` values in decision rules

If the planner returns invalid JSON on the first pass, the planner retries with the validation error fed back into the next prompt.

## Structured Logs

Each run directory under `.mta-runs/` now contains:

- `events.jsonl`: machine-readable progress and lifecycle events
- `summary.json`: final structured run summary, including failed-step log excerpts when available
- Per-session `session.log`, `stdout.log`, and `stderr.log`

This is in addition to the operator-facing terminal narration.

## Tuning Guidance

Recommended starting points:

- `MTA_PLANNER_MAX_ATTEMPTS=3`
  Good default for planner reliability without causing excessive duplicate model calls.
- `MTA_MAX_ITERATIONS=60`
  Fine for short and medium workflows. Increase to `120` for long multi-stage benchmark runs.
- `MTA_TIMEOUT_S=300`
  Good general-purpose default. Raise to `900` or higher for slow model downloads, container builds, or large warmups.
- `MTA_CAPTURE_LINES=300`
  Good balance for readiness checks. Increase if the target system emits long multi-line banners before the signal you need.
- `MTA_SESSION_BUFFER_MAX_CHARS=200000`
  Suitable for ordinary service logs. Increase for very noisy benchmark output when waits may need to inspect more history.
- `MTA_POLL_INTERVAL_S=1.0`
  Good default for responsiveness without over-polling. Lower it only if you need tighter feedback on short waits.

## Example

See [examples/sample_workflow.json](/Users/nickit/Desktop/workspace/model-test-agent/examples/sample_workflow.json) and [examples/sample_benchmark.md](/Users/nickit/Desktop/workspace/model-test-agent/examples/sample_benchmark.md).

## Limits

- Interactive apps are controlled through terminal keystroke injection, so the workflow needs explicit keystrokes for deterministic behavior.
- SSH authentication is expected to be preconfigured, or the workflow must include the required interactive steps.
- The planner is only as good as the source document. Complex runbooks should still be reviewed before execution.
