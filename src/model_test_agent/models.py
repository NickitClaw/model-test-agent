from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Union


class SessionTransport(str, Enum):
    LOCAL = "local"
    SSH = "ssh"
    DOCKER_EXEC = "docker_exec"
    DOCKER_RUN = "docker_run"


class StepKind(str, Enum):
    COMMAND = "command"
    WAIT = "wait"
    PROBE = "probe"
    SEND_KEYS = "send_keys"
    SLEEP = "sleep"
    BARRIER = "barrier"
    CAPTURE = "capture"
    DECISION = "decision"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    BACKGROUND = "background"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    raise TypeError(f"Expected list, got {type(value)!r}")


@dataclass
class SessionSpec:
    name: str
    transport: SessionTransport = SessionTransport.LOCAL
    shell: str = "/bin/bash"
    workdir: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    connect_ready_pattern: str | None = None
    startup_commands: list[str] = field(default_factory=list)
    ssh_host: str | None = None
    ssh_user: str | None = None
    ssh_port: int = 22
    docker_container: str | None = None
    docker_image: str | None = None
    docker_run_args: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "SessionSpec":
        return cls(
            name=name,
            transport=SessionTransport(data.get("transport", SessionTransport.LOCAL.value)),
            shell=str(data.get("shell", "/bin/bash")),
            workdir=data.get("workdir"),
            env={str(k): str(v) for k, v in data.get("env", {}).items()},
            connect_ready_pattern=data.get("connect_ready_pattern"),
            startup_commands=_list(data.get("startup_commands")),
            ssh_host=data.get("ssh_host"),
            ssh_user=data.get("ssh_user"),
            ssh_port=int(data.get("ssh_port", 22)),
            docker_container=data.get("docker_container"),
            docker_image=data.get("docker_image"),
            docker_run_args=_list(data.get("docker_run_args")),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("name", None)
        payload["transport"] = self.transport.value
        return payload


@dataclass
class StepBase:
    id: str
    kind: StepKind
    title: str
    session: str | None = None
    depends_on: list[str] = field(default_factory=list)
    description: str = ""
    continue_on_error: bool = False
    timeout_s: int | None = None
    retries: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["kind"] = self.kind.value
        return payload


@dataclass
class CommandStep(StepBase):
    command: str = ""
    background: bool = False
    ready_pattern: str | None = None
    success_patterns: list[str] = field(default_factory=list)
    fail_patterns: list[str] = field(default_factory=list)
    capture_lines: int = 300


@dataclass
class WaitStep(StepBase):
    pattern: str = ""
    fail_patterns: list[str] = field(default_factory=list)
    capture_lines: int = 300
    background: bool = False


@dataclass
class ProbeStep(StepBase):
    command: str = ""
    interval_s: float = 1.0
    expect_exit_code: int = 0
    success_patterns: list[str] = field(default_factory=list)
    fail_patterns: list[str] = field(default_factory=list)
    capture_lines: int = 120


@dataclass
class SendKeysStep(StepBase):
    keys: list[str] = field(default_factory=list)
    literal: bool = False
    press_enter: bool = False
    delay_s: float = 0.0


@dataclass
class SleepStep(StepBase):
    seconds: float = 1.0


@dataclass
class BarrierStep(StepBase):
    wait_for: list[str] = field(default_factory=list)
    poll_interval_s: float = 1.0


@dataclass
class CaptureStep(StepBase):
    source_session: str | None = None
    lines: int = 300


@dataclass
class DecisionRule:
    pattern: str
    action: str
    target_step: str | None = None
    note: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DecisionRule":
        return cls(
            pattern=str(data["pattern"]),
            action=str(data["action"]),
            target_step=data.get("target_step"),
            note=str(data.get("note", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DecisionStep(StepBase):
    source_session: str | None = None
    llm_prompt: str | None = None
    max_output_chars: int = 4000
    default_action: str = "continue"
    rules: list[DecisionRule] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["rules"] = [rule.to_dict() for rule in self.rules]
        return payload


WorkflowStep = Union[
    CommandStep,
    WaitStep,
    ProbeStep,
    SendKeysStep,
    SleepStep,
    BarrierStep,
    CaptureStep,
    DecisionStep,
]


def step_from_dict(data: dict[str, Any]) -> WorkflowStep:
    kind = StepKind(str(data["kind"]))
    common = dict(
        id=str(data["id"]),
        kind=kind,
        title=str(data.get("title", data["id"])),
        session=data.get("session"),
        depends_on=_list(data.get("depends_on")),
        description=str(data.get("description", "")),
        continue_on_error=bool(data.get("continue_on_error", False)),
        timeout_s=int(data["timeout_s"]) if data.get("timeout_s") is not None else None,
        retries=int(data.get("retries", 0)),
    )
    if kind is StepKind.COMMAND:
        return CommandStep(
            **common,
            command=str(data["command"]),
            background=bool(data.get("background", False)),
            ready_pattern=data.get("ready_pattern"),
            success_patterns=_list(data.get("success_patterns")),
            fail_patterns=_list(data.get("fail_patterns")),
            capture_lines=int(data.get("capture_lines", 300)),
        )
    if kind is StepKind.WAIT:
        return WaitStep(
            **common,
            pattern=str(data["pattern"]),
            fail_patterns=_list(data.get("fail_patterns")),
            capture_lines=int(data.get("capture_lines", 300)),
            background=bool(data.get("background", False)),
        )
    if kind is StepKind.PROBE:
        return ProbeStep(
            **common,
            command=str(data["command"]),
            interval_s=float(data.get("interval_s", 1.0)),
            expect_exit_code=int(data.get("expect_exit_code", 0)),
            success_patterns=_list(data.get("success_patterns")),
            fail_patterns=_list(data.get("fail_patterns")),
            capture_lines=int(data.get("capture_lines", 120)),
        )
    if kind is StepKind.SEND_KEYS:
        return SendKeysStep(
            **common,
            keys=_list(data.get("keys")),
            literal=bool(data.get("literal", False)),
            press_enter=bool(data.get("press_enter", False)),
            delay_s=float(data.get("delay_s", 0.0)),
        )
    if kind is StepKind.SLEEP:
        return SleepStep(**common, seconds=float(data.get("seconds", 1.0)))
    if kind is StepKind.BARRIER:
        return BarrierStep(
            **common,
            wait_for=_list(data.get("wait_for")),
            poll_interval_s=float(data.get("poll_interval_s", 1.0)),
        )
    if kind is StepKind.CAPTURE:
        return CaptureStep(
            **common,
            source_session=data.get("source_session"),
            lines=int(data.get("lines", 300)),
        )
    if kind is StepKind.DECISION:
        return DecisionStep(
            **common,
            source_session=data.get("source_session"),
            llm_prompt=data.get("llm_prompt"),
            max_output_chars=int(data.get("max_output_chars", 4000)),
            default_action=str(data.get("default_action", "continue")),
            rules=[DecisionRule.from_dict(item) for item in data.get("rules", [])],
        )
    raise ValueError(f"Unsupported step kind: {kind}")


@dataclass
class WorkflowSpec:
    name: str
    objective: str
    description: str = ""
    sessions: dict[str, SessionSpec] = field(default_factory=dict)
    steps: list[WorkflowStep] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowSpec":
        sessions = {
            name: SessionSpec.from_dict(name, session_data)
            for name, session_data in data.get("sessions", {}).items()
        }
        steps = [step_from_dict(item) for item in data.get("steps", [])]
        return cls(
            name=str(data["name"]),
            objective=str(data.get("objective", "")),
            description=str(data.get("description", "")),
            sessions=sessions,
            steps=steps,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "objective": self.objective,
            "description": self.description,
            "sessions": {name: spec.to_dict() for name, spec in self.sessions.items()},
            "steps": [step.to_dict() for step in self.steps],
        }

    def step_map(self) -> dict[str, WorkflowStep]:
        return {step.id: step for step in self.steps}


@dataclass
class StepResult:
    step_id: str
    status: StepStatus
    summary: str
    output: str = ""
    background_task_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload
