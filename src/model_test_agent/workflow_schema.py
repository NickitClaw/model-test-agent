from __future__ import annotations

from typing import Any

from .models import SessionTransport, StepKind


def get_workflow_json_schema() -> dict[str, Any]:
    session_transport_values = [item.value for item in SessionTransport]
    step_kind_values = [item.value for item in StepKind]

    step_base = {
        "type": "object",
        "required": ["id", "kind", "title"],
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "kind": {"type": "string", "enum": step_kind_values},
            "title": {"type": "string", "minLength": 1},
            "session": {"type": ["string", "null"]},
            "depends_on": {"type": "array", "items": {"type": "string"}},
            "description": {"type": "string"},
            "continue_on_error": {"type": "boolean"},
            "timeout_s": {"type": ["integer", "null"], "minimum": 1},
            "retries": {"type": "integer", "minimum": 0},
            "metadata": {"type": "object"},
        },
        "additionalProperties": True,
    }
    session_schema = {
        "type": "object",
        "properties": {
            "transport": {"type": "string", "enum": session_transport_values},
            "shell": {"type": "string"},
            "workdir": {"type": ["string", "null"]},
            "env": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
            "connect_ready_pattern": {"type": ["string", "null"]},
            "startup_commands": {"type": "array", "items": {"type": "string"}},
            "ssh_host": {"type": ["string", "null"]},
            "ssh_user": {"type": ["string", "null"]},
            "ssh_port": {"type": "integer", "minimum": 1},
            "docker_container": {"type": ["string", "null"]},
            "docker_image": {"type": ["string", "null"]},
            "docker_run_args": {"type": "array", "items": {"type": "string"}},
            "metadata": {"type": "object"},
        },
        "additionalProperties": False,
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://model-test-agent.local/schema/workflow.json",
        "title": "Model Test Agent Workflow",
        "type": "object",
        "required": ["name", "objective", "sessions", "steps"],
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "objective": {"type": "string"},
            "description": {"type": "string"},
            "metadata": {"type": "object"},
            "sessions": {
                "type": "object",
                "propertyNames": {"type": "string", "minLength": 1},
                "additionalProperties": session_schema,
            },
            "steps": {
                "type": "array",
                "items": {
                    "anyOf": [
                        {
                            "allOf": [
                                step_base,
                                {
                                    "properties": {
                                        "kind": {"const": "command"},
                                        "command": {"type": "string", "minLength": 1},
                                        "background": {"type": "boolean"},
                                        "ready_pattern": {"type": ["string", "null"]},
                                        "success_patterns": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                        "fail_patterns": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                        "capture_lines": {"type": "integer", "minimum": 1},
                                    },
                                    "required": ["command"],
                                },
                            ]
                        },
                        {
                            "allOf": [
                                step_base,
                                {
                                    "properties": {
                                        "kind": {"const": "wait"},
                                        "pattern": {"type": "string", "minLength": 1},
                                        "fail_patterns": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                        "capture_lines": {"type": "integer", "minimum": 1},
                                        "background": {"type": "boolean"},
                                    },
                                    "required": ["pattern"],
                                },
                            ]
                        },
                        {
                            "allOf": [
                                step_base,
                                {
                                    "properties": {
                                        "kind": {"const": "probe"},
                                        "command": {"type": "string", "minLength": 1},
                                        "interval_s": {"type": "number", "exclusiveMinimum": 0},
                                        "expect_exit_code": {"type": "integer"},
                                        "success_patterns": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                        "fail_patterns": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                        "capture_lines": {"type": "integer", "minimum": 1},
                                    },
                                    "required": ["command"],
                                },
                            ]
                        },
                        {
                            "allOf": [
                                step_base,
                                {
                                    "properties": {
                                        "kind": {"const": "send_keys"},
                                        "keys": {"type": "array", "items": {"type": "string"}},
                                        "literal": {"type": "boolean"},
                                        "press_enter": {"type": "boolean"},
                                        "delay_s": {"type": "number", "minimum": 0},
                                    },
                                },
                            ]
                        },
                        {
                            "allOf": [
                                step_base,
                                {
                                    "properties": {
                                        "kind": {"const": "sleep"},
                                        "seconds": {"type": "number", "exclusiveMinimum": 0},
                                    },
                                },
                            ]
                        },
                        {
                            "allOf": [
                                step_base,
                                {
                                    "properties": {
                                        "kind": {"const": "barrier"},
                                        "wait_for": {"type": "array", "items": {"type": "string"}},
                                        "poll_interval_s": {
                                            "type": "number",
                                            "exclusiveMinimum": 0,
                                        },
                                    },
                                },
                            ]
                        },
                        {
                            "allOf": [
                                step_base,
                                {
                                    "properties": {
                                        "kind": {"const": "capture"},
                                        "source_session": {"type": ["string", "null"]},
                                        "lines": {"type": "integer", "minimum": 1},
                                    },
                                },
                            ]
                        },
                        {
                            "allOf": [
                                step_base,
                                {
                                    "properties": {
                                        "kind": {"const": "decision"},
                                        "source_session": {"type": ["string", "null"]},
                                        "llm_prompt": {"type": ["string", "null"]},
                                        "max_output_chars": {"type": "integer", "minimum": 1},
                                        "default_action": {"type": "string"},
                                        "rules": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "required": ["pattern", "action"],
                                                "properties": {
                                                    "pattern": {"type": "string", "minLength": 1},
                                                    "action": {"type": "string", "minLength": 1},
                                                    "target_step": {"type": ["string", "null"]},
                                                    "note": {"type": "string"},
                                                },
                                                "additionalProperties": False,
                                            },
                                        },
                                    },
                                },
                            ]
                        },
                    ]
                },
            },
        },
        "additionalProperties": False,
    }
