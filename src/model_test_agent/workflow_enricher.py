from __future__ import annotations

import re
import shlex
from copy import deepcopy
from dataclasses import replace
from typing import Any, Iterable

from .models import BarrierStep, CommandStep, ProbeStep, SendKeysStep, SleepStep, StepKind, WaitStep, WorkflowSpec, WorkflowStep


SERVER_HINTS = (
    "server",
    "serve",
    "uvicorn",
    "gunicorn",
    "http.server",
    "flask run",
    "fastapi",
    "vllm",
    "listen",
    "startup-delay",
)

CLIENT_HINTS = (
    "curl ",
    "wget ",
    "http://",
    "https://",
    "--base-url",
    "benchmark",
    "client",
    "request",
)

AMBIGUOUS_READY_PATTERNS = {
    "listening",
    "listening on",
    "ready",
    "started",
    "running",
    "up",
}


class WorkflowEnricher:
    def enrich(self, workflow: WorkflowSpec) -> WorkflowSpec:
        enriched = WorkflowSpec.from_dict(workflow.to_dict())
        original_steps = list(enriched.steps)
        existing_ids = {step.id for step in original_steps}
        new_steps: list[WorkflowStep] = []
        changes: list[dict[str, Any]] = []

        for index, step in enumerate(original_steps):
            new_steps.append(step)
            if not isinstance(step, CommandStep):
                continue
            if not self._is_server_candidate(step, original_steps[index + 1 :]):
                continue

            existing_wait = self._has_existing_wait(original_steps, step.id)
            consumers = self._find_consumers(step, original_steps[index + 1 :])
            if not consumers and not existing_wait:
                continue

            step.background = True
            self._annotate_existing_step(
                step,
                action="set_background",
                reason=(
                    "Marked this launch command as background because later steps depend on the service staying alive "
                    "while they run."
                ),
                details={"downstream_steps": [item.id for item in consumers]},
            )
            self._record_change(
                changes,
                kind="step_updated",
                target=step.id,
                reason="Marked launch step as background so downstream dependent steps can run.",
                details={"downstream_steps": [item.id for item in consumers]},
            )
            if existing_wait:
                if self._ready_pattern_is_ambiguous(step.ready_pattern):
                    previous_pattern = step.ready_pattern
                    step.ready_pattern = None
                    self._annotate_existing_step(
                        step,
                        action="clear_ready_pattern",
                        reason=(
                            "Cleared an ambiguous ready pattern because an explicit downstream wait or probe already "
                            "handles readiness more reliably."
                        ),
                        details={"previous_ready_pattern": previous_pattern},
                    )
                    self._record_change(
                        changes,
                        kind="step_updated",
                        target=step.id,
                        reason="Cleared ambiguous ready_pattern because an explicit wait/probe already exists.",
                        details={"previous_ready_pattern": previous_pattern},
                    )
                continue
            inferred_wait = self._build_inferred_wait(step, consumers, existing_ids)
            if step.ready_pattern and not self._should_replace_ready_pattern(step, inferred_wait):
                self._attach_dependencies(consumers, step.id)
                self._annotate_consumers_with_dependency(
                    consumers,
                    dep_id=step.id,
                    reason="Linked downstream steps to the existing launch step because its ready_pattern already acts as the readiness gate.",
                    changes=changes,
                )
                continue
            if inferred_wait:
                previous_pattern = step.ready_pattern
                step.ready_pattern = None
                if previous_pattern:
                    self._annotate_existing_step(
                        step,
                        action="clear_ready_pattern",
                        reason=(
                            "Replaced an ambiguous ready pattern with a deterministic inferred probe built from the "
                            "downstream client traffic."
                        ),
                        details={"previous_ready_pattern": previous_pattern, "replacement_step": inferred_wait.id},
                    )
                    self._record_change(
                        changes,
                        kind="step_updated",
                        target=step.id,
                        reason="Replaced ambiguous ready_pattern with an inferred readiness probe.",
                        details={"previous_ready_pattern": previous_pattern, "replacement_step": inferred_wait.id},
                    )
                self._annotate_inserted_step(
                    inferred_wait,
                    reason=(
                        "Added an inferred readiness probe because downstream client commands target the launched "
                        "endpoint and the runbook did not define a reliable explicit wait."
                    ),
                    details={
                        "launch_step": step.id,
                        "consumer_steps": [item.id for item in consumers],
                    },
                )
                self._record_change(
                    changes,
                    kind="step_inserted",
                    target=inferred_wait.id,
                    reason="Inserted inferred readiness probe for a downstream client path.",
                    details={
                        "launch_step": step.id,
                        "consumer_steps": [item.id for item in consumers],
                    },
                )
                new_steps.append(inferred_wait)
                self._attach_dependencies(consumers, inferred_wait.id)
                self._annotate_consumers_with_dependency(
                    consumers,
                    dep_id=inferred_wait.id,
                    reason="Added dependency on the inferred readiness probe before downstream client work can start.",
                    changes=changes,
                )
            elif not self._attach_sleep_fallback(step, consumers, new_steps, existing_ids, changes):
                self._attach_dependencies(consumers, step.id)
                self._annotate_consumers_with_dependency(
                    consumers,
                    dep_id=step.id,
                    reason="Added dependency on the launch step because the runbook omitted a more specific readiness gate.",
                    changes=changes,
                )

        enriched.steps = new_steps
        self._separate_network_client_sessions(enriched, changes)
        self._append_cleanup_steps(enriched, existing_ids, changes)
        self._finalize_workflow_metadata(enriched, changes)
        return enriched

    def _is_server_candidate(self, step: CommandStep, later_steps: list[WorkflowStep]) -> bool:
        haystack = f"{step.title} {step.command}".lower()
        if step.background or step.ready_pattern:
            return True
        if any(token in haystack for token in SERVER_HINTS):
            return True
        if self._extract_startup_delay(step.command) is not None:
            return True
        launch_endpoint = self._extract_host_port(step.command)
        if launch_endpoint is None:
            return False
        return any(self._step_targets_launch_endpoint(step, item) for item in later_steps)

    def _find_consumers(self, launch_step: CommandStep, later_steps: list[WorkflowStep]) -> list[WorkflowStep]:
        consumers: list[WorkflowStep] = []
        for step in later_steps:
            if self._step_targets_launch_endpoint(launch_step, step):
                consumers.append(step)
                continue
            if (
                isinstance(step, CommandStep)
                and step.session
                and launch_step.session
                and step.session != launch_step.session
                and self._has_strong_server_identity(launch_step)
                and not self._has_strong_server_identity(step)
                and not self._looks_like_network_client(step)
            ):
                consumers.append(step)
        return consumers

    def _looks_like_network_client(self, step: WorkflowStep) -> bool:
        command = self._step_command_text(step)
        if not command:
            return False
        haystack = f"{getattr(step, 'title', '')} {command}".lower()
        return any(token in haystack for token in CLIENT_HINTS)

    def _attach_dependencies(self, consumers: Iterable[CommandStep], dep_id: str) -> None:
        for consumer in consumers:
            if dep_id not in consumer.depends_on:
                consumer.depends_on.append(dep_id)

    def _build_inferred_wait(
        self,
        launch_step: CommandStep,
        consumers: list[WorkflowStep],
        existing_ids: set[str],
    ) -> ProbeStep | None:
        safe_curl = next(
            (
                step
                for step in consumers
                if isinstance(step, CommandStep) and self._is_safe_curl(step.command)
            ),
            None,
        )
        if safe_curl:
            probe_command = safe_curl.command
            probe_session = safe_curl.session or launch_step.session
            success_patterns = list(safe_curl.success_patterns)
            fail_patterns = list(safe_curl.fail_patterns)
        else:
            url = None
            probe_session = None
            for step in consumers:
                command = self._step_command_text(step)
                if not command:
                    continue
                url = self._extract_url(command)
                if url:
                    probe_session = getattr(step, "session", None) or launch_step.session
                    break
            if url:
                probe_command = f"curl --fail --silent {shlex.quote(url)}"
                success_patterns = []
                fail_patterns = ["curl:", "Connection refused", "Failed to connect"]
            else:
                endpoint = self._extract_host_port_from_steps([launch_step, *consumers])
                if not endpoint:
                    return None
                host, port = endpoint
                code = (
                    "import socket; "
                    f"s=socket.create_connection(({host!r}, {port}), 2); "
                    "s.close()"
                )
                probe_command = f"python3 -c {shlex.quote(code)}"
                first_consumer_session = getattr(consumers[0], "session", None) if consumers else None
                probe_session = first_consumer_session or launch_step.session
                success_patterns = []
                fail_patterns = []
        probe_id = self._unique_id(f"{launch_step.id}_wait_ready", existing_ids)
        timeout_s = int((self._extract_startup_delay(launch_step.command) or 30.0) + 30.0)
        return ProbeStep(
            id=probe_id,
            kind=StepKind.PROBE,
            title=f"Inferred readiness probe for {launch_step.title}",
            session=probe_session,
            depends_on=[launch_step.id],
            command=probe_command,
            interval_s=1.0,
            expect_exit_code=0,
            success_patterns=success_patterns,
            fail_patterns=fail_patterns,
            capture_lines=120,
            timeout_s=timeout_s,
            description="Auto-inferred wait step based on downstream client commands.",
        )

    def _attach_sleep_fallback(
        self,
        launch_step: CommandStep,
        consumers: list[WorkflowStep],
        new_steps: list[WorkflowStep],
        existing_ids: set[str],
        changes: list[dict[str, Any]],
    ) -> bool:
        delay = self._extract_startup_delay(launch_step.command)
        if delay is None:
            return False
        step_id = self._unique_id(f"{launch_step.id}_sleep_ready", existing_ids)
        sleep_step = SleepStep(
            id=step_id,
            kind=StepKind.SLEEP,
            title=f"Inferred wait for {launch_step.title}",
            depends_on=[launch_step.id],
            seconds=delay,
            description="Auto-inferred sleep based on startup delay in the launch command.",
        )
        self._annotate_inserted_step(
            sleep_step,
            reason=(
                "Added an inferred sleep because the launch command declares an explicit startup delay and the "
                "runbook omitted a dedicated readiness step."
            ),
            details={"launch_step": launch_step.id, "startup_delay_s": delay},
        )
        self._record_change(
            changes,
            kind="step_inserted",
            target=sleep_step.id,
            reason="Inserted inferred sleep based on the launch command startup delay.",
            details={"launch_step": launch_step.id, "startup_delay_s": delay},
        )
        new_steps.append(sleep_step)
        self._attach_dependencies(consumers, step_id)
        self._annotate_consumers_with_dependency(
            consumers,
            dep_id=step_id,
            reason="Added dependency on the inferred sleep derived from the launch command startup delay.",
            changes=changes,
        )
        return True

    def _append_cleanup_steps(
        self,
        workflow: WorkflowSpec,
        existing_ids: set[str],
        changes: list[dict[str, Any]],
    ) -> None:
        background_servers = [
            step
            for step in workflow.steps
            if isinstance(step, CommandStep) and step.background and self._is_server_candidate(step, [])
        ]
        if not background_servers:
            return
        for launch_step in background_servers:
            if self._has_explicit_cleanup(workflow.steps, launch_step):
                continue
            dep_ids = [
                step.id
                for step in workflow.steps
                if step.id != launch_step.id and step.kind not in {StepKind.SEND_KEYS}
            ]
            cleanup_id = self._unique_id(f"{launch_step.id}_stop", existing_ids)
            cleanup_step = SendKeysStep(
                id=cleanup_id,
                kind=StepKind.SEND_KEYS,
                title=f"Inferred stop for {launch_step.title}",
                session=launch_step.session,
                depends_on=dep_ids,
                keys=["C-c"],
                description="Auto-inferred cleanup step for a background server.",
            )
            self._annotate_inserted_step(
                cleanup_step,
                reason=(
                    "Added an inferred Ctrl-C cleanup step because the server was converted into a long-lived "
                    "background process and the runbook omitted an explicit shutdown."
                ),
                details={"launch_step": launch_step.id},
            )
            self._record_change(
                changes,
                kind="step_inserted",
                target=cleanup_step.id,
                reason="Inserted inferred cleanup step for a background server.",
                details={"launch_step": launch_step.id},
            )
            workflow.steps.append(cleanup_step)

    def _separate_network_client_sessions(self, workflow: WorkflowSpec, changes: list[dict[str, Any]]) -> None:
        for step in workflow.steps:
            if not isinstance(step, CommandStep) or not step.background or not step.session:
                continue
            if not self._is_server_candidate(step, []):
                continue
            launch_session = step.session
            target_session = None
            for item in workflow.steps:
                if item is step:
                    continue
                if getattr(item, "session", None) != launch_session:
                    continue
                if isinstance(item, CommandStep) and self._should_move_command_off_server_session(item, step):
                    target_session = target_session or self._ensure_cloned_session(workflow, launch_session, changes)
                    previous_session = item.session
                    item.session = target_session
                    self._annotate_existing_step(
                        item,
                        action="move_session",
                        reason=(
                            "Moved this client or cleanup command into a cloned session so it does not compete with "
                            "the long-lived server process in the original terminal."
                        ),
                        details={"previous_session": previous_session, "new_session": target_session},
                    )
                    self._record_change(
                        changes,
                        kind="step_updated",
                        target=item.id,
                        reason="Moved client/cleanup step into a cloned session to keep the server session dedicated.",
                        details={"previous_session": previous_session, "new_session": target_session},
                    )
                elif (
                    isinstance(item, ProbeStep)
                    and launch_session in workflow.sessions
                    and self._is_network_probe(item, step)
                ):
                    target_session = target_session or self._ensure_cloned_session(workflow, launch_session, changes)
                    previous_session = item.session
                    item.session = target_session
                    self._annotate_existing_step(
                        item,
                        action="move_session",
                        reason=(
                            "Moved this probe into a cloned session so readiness checks can run independently of the "
                            "server process that owns the original terminal."
                        ),
                        details={"previous_session": previous_session, "new_session": target_session},
                    )
                    self._record_change(
                        changes,
                        kind="step_updated",
                        target=item.id,
                        reason="Moved probe step into a cloned session to keep readiness checks independent.",
                        details={"previous_session": previous_session, "new_session": target_session},
                    )

    def _ensure_cloned_session(
        self,
        workflow: WorkflowSpec,
        source_session: str,
        changes: list[dict[str, Any]],
    ) -> str:
        base_name = f"{source_session}_client"
        candidate = base_name
        suffix = 2
        while candidate in workflow.sessions:
            candidate = f"{base_name}_{suffix}"
            suffix += 1
        cloned = replace(
            workflow.sessions[source_session],
            name=candidate,
            metadata=deepcopy(workflow.sessions[source_session].metadata),
        )
        self._set_inserted_provenance(
            cloned.metadata,
            reason=(
                "Cloned this session from the original server session so client or probe commands can run in a "
                "separate persistent shell."
            ),
            details={"source_session": source_session},
        )
        workflow.sessions[candidate] = cloned
        self._record_change(
            changes,
            kind="session_inserted",
            target=candidate,
            reason="Cloned a new client session from the original server session to separate long-lived server and client/probe work.",
            details={"source_session": source_session},
        )
        return candidate

    def _has_explicit_cleanup(self, steps: list[WorkflowStep], launch_step: CommandStep) -> bool:
        for step in steps:
            if isinstance(step, SendKeysStep) and step.session == launch_step.session and "C-c" in step.keys:
                return True
            if isinstance(step, CommandStep) and step.session == launch_step.session:
                lowered = step.command.lower()
                if "pkill" in lowered or "kill " in lowered or "killall" in lowered:
                    return True
        return False

    def _has_existing_wait(self, steps: list[WorkflowStep], launch_step_id: str) -> bool:
        for step in steps:
            if isinstance(step, ProbeStep) and launch_step_id in step.depends_on:
                return True
            if isinstance(step, WaitStep) and launch_step_id in step.depends_on:
                return True
            if isinstance(step, SleepStep) and launch_step_id in step.depends_on:
                return True
            if isinstance(step, BarrierStep) and launch_step_id in step.wait_for:
                return True
        return False

    def _extract_url(self, command: str) -> str | None:
        match = re.search(r"https?://[^\s'\"`]+", command)
        if match:
            return match.group(0)
        return None

    def _extract_host_port_from_steps(self, steps: list[WorkflowStep]) -> tuple[str, int] | None:
        for step in steps:
            command = self._step_command_text(step)
            if not command:
                continue
            endpoint = self._extract_host_port(command)
            if endpoint:
                return endpoint
        return None

    def _extract_host_port(self, command: str) -> tuple[str, int] | None:
        url = self._extract_url(command)
        if url:
            match = re.match(r"https?://([^/:]+)(?::(\d+))?", url)
            if match:
                host = match.group(1)
                port = int(match.group(2) or (443 if url.startswith("https://") else 80))
                return host, port
        match = re.search(r"--host\s+([^\s]+)", command)
        host = match.group(1) if match else "127.0.0.1"
        port_match = re.search(r"--port\s+(\d+)", command)
        if port_match:
            return host, int(port_match.group(1))
        docker_match = re.search(r"(?:^|\s)-p\s+(\d+):(\d+)", command)
        if docker_match:
            return host, int(docker_match.group(1))
        inline_match = re.search(r"([A-Za-z0-9_.-]+):(\d{2,5})", command)
        if inline_match:
            return inline_match.group(1), int(inline_match.group(2))
        return None

    def _extract_startup_delay(self, command: str) -> float | None:
        match = re.search(r"--startup-delay\s+([0-9]+(?:\.[0-9]+)?)", command)
        if match:
            return float(match.group(1))
        return None

    def _should_replace_ready_pattern(self, launch_step: CommandStep, inferred_wait: ProbeStep | None) -> bool:
        if not launch_step.ready_pattern or inferred_wait is None:
            return False
        return self._ready_pattern_is_ambiguous(launch_step.ready_pattern)

    @staticmethod
    def _ready_pattern_is_ambiguous(pattern: str | None) -> bool:
        if not pattern:
            return False
        normalized = re.sub(r"\s+", " ", pattern.strip().lower())
        return normalized in AMBIGUOUS_READY_PATTERNS

    def _is_safe_curl(self, command: str) -> bool:
        lowered = command.lower().strip()
        if not lowered.startswith("curl "):
            return False
        unsafe_tokens = (" -x ", "--request ", " -d ", "--data", "--data-binary", "--form")
        return not any(token in lowered for token in unsafe_tokens)

    def _is_network_probe(self, step: ProbeStep, launch_step: CommandStep | None = None) -> bool:
        lowered = step.command.lower()
        if not (self._is_safe_curl(step.command) or "http://" in lowered or "https://" in lowered):
            return False
        if launch_step is None:
            return True
        return self._step_targets_launch_endpoint(launch_step, step)

    def _should_move_command_off_server_session(self, step: CommandStep, launch_step: CommandStep) -> bool:
        if self._step_targets_launch_endpoint(launch_step, step):
            return True
        lowered = step.command.lower()
        return "pkill" in lowered or "killall" in lowered or re.search(r"(?:^|\s)kill\s", lowered) is not None

    def _step_targets_launch_endpoint(self, launch_step: CommandStep, step: WorkflowStep) -> bool:
        command = self._step_command_text(step)
        if not command:
            return False
        if not self._looks_like_network_client(step) and not isinstance(step, ProbeStep):
            return False
        launch_endpoint = self._extract_host_port(launch_step.command)
        target_endpoint = self._extract_host_port(command)
        if launch_endpoint and target_endpoint:
            return launch_endpoint == target_endpoint
        if launch_endpoint:
            url = self._extract_url(command)
            return bool(url)
        return self._has_strong_server_identity(launch_step)

    def _has_strong_server_identity(self, step: CommandStep) -> bool:
        haystack = f"{step.title} {step.command}".lower()
        return (
            step.background
            or bool(step.ready_pattern)
            or any(token in haystack for token in SERVER_HINTS)
            or self._extract_startup_delay(step.command) is not None
        )

    @staticmethod
    def _step_command_text(step: WorkflowStep) -> str | None:
        if isinstance(step, CommandStep):
            return step.command
        if isinstance(step, ProbeStep):
            return step.command
        return None

    def _unique_id(self, base: str, existing_ids: set[str]) -> str:
        candidate = re.sub(r"[^a-zA-Z0-9_]+", "_", base).strip("_") or "step"
        if candidate not in existing_ids:
            existing_ids.add(candidate)
            return candidate
        index = 2
        while f"{candidate}_{index}" in existing_ids:
            index += 1
        final = f"{candidate}_{index}"
        existing_ids.add(final)
        return final

    def _annotate_consumers_with_dependency(
        self,
        consumers: list[WorkflowStep],
        *,
        dep_id: str,
        reason: str,
        changes: list[dict[str, Any]],
    ) -> None:
        for consumer in consumers:
            if dep_id not in getattr(consumer, "depends_on", []):
                continue
            self._annotate_existing_step(
                consumer,
                action="add_dependency",
                reason=reason,
                details={"dependency": dep_id},
            )
            self._record_change(
                changes,
                kind="step_updated",
                target=consumer.id,
                reason=reason,
                details={"dependency": dep_id},
            )

    @staticmethod
    def _set_inserted_provenance(metadata: dict[str, Any], *, reason: str, details: dict[str, Any] | None = None) -> None:
        metadata["provenance"] = {"origin": "enricher", "reason": reason}
        if details:
            metadata["provenance"]["details"] = details

    def _annotate_inserted_step(
        self,
        step: WorkflowStep,
        *,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._set_inserted_provenance(step.metadata, reason=reason, details=details)

    def _annotate_existing_step(
        self,
        step: WorkflowStep,
        *,
        action: str,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        annotations = step.metadata.setdefault("annotations", [])
        note: dict[str, Any] = {"origin": "enricher", "action": action, "reason": reason}
        if details:
            note["details"] = details
        if note not in annotations:
            annotations.append(note)

    @staticmethod
    def _record_change(
        changes: list[dict[str, Any]],
        *,
        kind: str,
        target: str,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        change: dict[str, Any] = {"kind": kind, "target": target, "reason": reason}
        if details:
            change["details"] = details
        changes.append(change)

    def _finalize_workflow_metadata(self, workflow: WorkflowSpec, changes: list[dict[str, Any]]) -> None:
        enrichment = workflow.metadata.setdefault("enrichment", {})
        enrichment["applied"] = True
        enrichment["change_count"] = len(changes)
        enrichment["changes"] = changes
        if changes:
            inserted = sum(1 for item in changes if item["kind"] in {"step_inserted", "session_inserted"})
            updated = sum(1 for item in changes if item["kind"] == "step_updated")
            enrichment["summary"] = (
                f"Deterministic enrichment applied {len(changes)} change(s): "
                f"{inserted} inserted and {updated} updated."
            )
        else:
            enrichment["summary"] = "Deterministic enrichment made no changes."
