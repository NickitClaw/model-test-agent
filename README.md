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

## CLI

Plan a workflow from a Markdown/PDF document:

```bash
mta plan docs/benchmark.md --output workflow.json
```

Run a generated workflow through the agent:

```bash
mta run-workflow workflow.json
```

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

## Example

See [examples/sample_workflow.json](/Users/nickit/Desktop/workspace/model-test-agent/examples/sample_workflow.json) and [examples/sample_benchmark.md](/Users/nickit/Desktop/workspace/model-test-agent/examples/sample_benchmark.md).

## Limits

- Interactive apps are controlled through terminal keystroke injection, so the workflow needs explicit keystrokes for deterministic behavior.
- SSH authentication is expected to be preconfigured, or the workflow must include the required interactive steps.
- The planner is only as good as the source document. Complex runbooks should still be reviewed before execution.
