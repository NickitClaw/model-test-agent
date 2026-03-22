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

## Configuration

`model-test-agent` now supports layered configuration from both config files and environment variables.

Precedence is:

1. built-in defaults
2. user config at `~/.mta.toml`
3. nearest project config at `.mta.toml` in the current directory or an ancestor
4. environment variables

You can also point directly at a config file with `MTA_CONFIG=/path/to/file.toml`.

Minimal example:

```toml
[mta]
base_url = "http://127.0.0.1:18889/v1"
model = "gpt-5.4"
session_backend = "pty"
planner_max_attempts = 3
max_iterations = 60
timeout_s = 900
```

`mta doctor` prints the resolved config file paths so you can confirm which files were loaded.

Environment variables still override config files. Supported variables include:

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

Legacy `MBA_*` environment variables are still accepted for compatibility.

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
- `metadata`: optional planning and enrichment provenance for explainability

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

Generated workflows now keep explicit explainability metadata:

- `workflow.metadata.planning`
  Records the planner model, source document path, local document analysis, and the planning attempt that produced the workflow skeleton.
- `workflow.metadata.enrichment`
  Records deterministic post-processing changes such as inserted probes, inferred cleanup steps, cloned sessions, or dependency rewrites.
- `session.metadata` and `step.metadata`
  Record provenance and per-item annotations, so you can tell which items came directly from the planner and which ones were inserted or modified by deterministic enrichment.

The project now exposes a formal JSON Schema through `mta schema`. Internally, planned and loaded workflows are also validated for:

- Duplicate step ids
- Unknown session references
- Unknown dependencies in `depends_on`
- Unknown `wait_for` targets in barrier steps
- Unknown `target_step` values in decision rules
- Dependency cycles, including cycles introduced through barrier `wait_for`

If the planner returns invalid JSON on the first pass, the planner retries with the validation error fed back into the next prompt.

Step execution is now dispatched through a handler registry rather than one monolithic runner method. Built-in handlers cover all current step kinds, and new step kinds can be added by registering another handler.

## Transport Capability Matrix

All transports use the same persistent-session runtime, but their operational boundaries are different:

- `local`
  Full support for persistent shells, background watchers, readiness waits, interactive keystrokes, and per-session stdout/stderr/session logs.
- `ssh`
  Full support after the SSH session is established with `ssh -tt`. This assumes network reachability and either preconfigured authentication or explicit interactive workflow steps to complete login.
- `docker_exec`
  Full support for an already-running container. The runtime attaches with `docker exec -it <container> <shell>` and then treats it like a persistent interactive shell.
- `docker_run`
  Full support for an interactive shell inside a newly launched container. The container lifecycle is tied to the session, so if the container exits the session is gone as well.

Operational notes:

- Background watchers and waits observe terminal output after the transport shell is attached. They do not inspect Docker daemon logs or SSH control-plane events.
- `connect_ready_pattern` is optional but recommended for `ssh`, `docker_exec`, and `docker_run` when the remote shell emits a known prompt or banner.
- SSH host keys, passwords, MFA prompts, registry logins, and similar environment-specific prerequisites are not automated unless the workflow includes explicit steps for them.

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
  Suitable for ordinary service logs. This controls the interactive tail kept in memory for capture and display. PTY waits and command completion now also scan an internal append-only transcript log, so readiness matching is less likely to miss early output even when the in-memory tail is noisy.
- `MTA_POLL_INTERVAL_S=1.0`
  Good default for responsiveness without over-polling. Lower it only if you need tighter feedback on short waits.

## Example

See [examples/sample_workflow.json](/Users/nickit/Desktop/workspace/model-test-agent/examples/sample_workflow.json) and [examples/sample_benchmark.md](/Users/nickit/Desktop/workspace/model-test-agent/examples/sample_benchmark.md).

## Limits

- Interactive apps are controlled through terminal keystroke injection, so the workflow needs explicit keystrokes for deterministic behavior.
- SSH authentication is expected to be preconfigured, or the workflow must include the required interactive steps.
- The planner is only as good as the source document. Complex runbooks should still be reviewed before execution.
