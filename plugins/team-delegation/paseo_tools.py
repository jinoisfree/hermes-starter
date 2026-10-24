"""Structured access to the installed, local Paseo CLI; no workflow policy."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from pathlib import Path
from typing import Any

ACTIONS = ("status", "projects", "workspaces", "agents", "inspect", "logs", "run", "send", "stop")
MUTATIONS = {"run", "send", "stop"}
SCHEMA = {
    "name": "paseo",
    "description": (
        "Operate the local Paseo app: list projects/workspaces/agents, inspect an agent, read logs, "
        "start a conversation, send a prompt, or stop one agent. Follow the user's instructions "
        "for how to use Paseo. run requires workspace_id, provider and prompt; send requires "
        "agent_id and prompt. inspect/logs/stop require the full agent_id from agents. "
        "run/send return immediately after acceptance, not after task completion. "
        "Use inspect/logs to retrieve progress and results. No agent permission modes are changed."
    ),
    "parameters": {
        "type": "object", "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "agent_id": {"type": "string", "description": "Full Paseo agent UUID from agents."},
            "workspace_id": {"type": "string", "description": "Existing workspace ID from workspaces; required for run."},
            "provider": {"type": "string", "description": "Provider from status, e.g. claude or codex; required for run."},
            "prompt": {"type": "string", "description": "Task or message for run/send."},
            "title": {"type": "string", "description": "Optional new conversation title for run."},
            "model": {"type": "string", "description": "Optional model for run; otherwise Paseo's default."},
            "thinking": {"type": "string", "description": "Optional thinking option ID for run."},
            "tail": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Recent log entries, default 20."},
            "filter": {"type": "string", "enum": ["text", "tools", "errors", "permissions"],
                       "description": "Log type, default text."},
        }, "required": ["action"], "additionalProperties": False,
    },
}
_FIELDS = {
    "status": set(), "projects": set(), "workspaces": set(), "agents": set(),
    "inspect": {"agent_id"}, "logs": {"agent_id", "tail", "filter"},
    "run": {"workspace_id", "provider", "prompt", "title", "model", "thinking"},
    "send": {"agent_id", "prompt"}, "stop": {"agent_id"},
}


def _text(args: dict, key: str, limit: int) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"{key} must be nonempty text of at most {limit} characters")
    return value


def _arguments(args: dict[str, Any]) -> list[str]:
    action = args.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValueError("Unknown Paseo action")
    if set(args) - _FIELDS[action] - {"action"}:
        raise ValueError(f"Unexpected arguments for {action}")
    if action in {"inspect", "logs", "send", "stop"}:
        agent_id = _text(args, "agent_id", 36)
        if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", agent_id):
            raise ValueError("agent_id must be the full UUID from agents")
        command = [action, agent_id]
        if action == "logs":
            tail, kind = args.get("tail", 20), args.get("filter", "text")
            if type(tail) is not int or not 1 <= tail <= 50 or not isinstance(kind, str) or kind not in {"text", "tools", "errors", "permissions"}:
                raise ValueError("logs needs tail 1..50 and a supported filter")
            command += ["--tail", str(tail), "--filter", kind]
        if action == "send":
            command += ["--no-wait", "--", _text(args, "prompt", 24_000)]
        return command
    if action == "run":
        workspace = _text(args, "workspace_id", 100)
        if not re.fullmatch(r"wks_[a-zA-Z0-9_-]+", workspace):
            raise ValueError("workspace_id must be an existing ID from workspaces")
        command = ["run", "--background", "--workspace", workspace,
                   "--provider", _text(args, "provider", 120)]
        for key in ("title", "model", "thinking"):
            if key in args:
                command += [f"--{key}", _text(args, key, 200)]
        return [*command, "--", _text(args, "prompt", 24_000)]
    return {"status": ["status"], "projects": ["project", "ls"],
            "workspaces": ["workspace", "ls"], "agents": ["ls", "--global"]}[action]


def _run(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, cwd=Path.home(), env=env, start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=45)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def call(args: dict[str, Any], settings: dict[str, Any], *, runner=None) -> str:
    action = args.get("action")
    action_name = action if isinstance(action, str) else "invalid"
    def failure(message: str, *, uncertain: bool = False, **extra: Any) -> str:
        return json.dumps({"status": "unknown" if uncertain else "failed", "action": action_name,
                           "error": message, **extra}, ensure_ascii=False)
    try:
        arguments = _arguments(args)
        binary = Path(settings.get("binary") or "/opt/homebrew/bin/paseo").expanduser()
        home = Path(settings.get("home") or Path.home() / ".paseo").expanduser()
        if not binary.is_file() or not os.access(binary, os.X_OK):
            return failure("Paseo CLI is unavailable; check paseo settings")
        # An explicit local home prevents gateway/parent-agent environment from changing the target.
        env = {key: value for key, value in os.environ.items()
               if key in {"HOME", "PATH", "TMPDIR", "USER", "LOGNAME", "LANG", "LC_ALL", "SHELL"}}
        result = (runner or _run)([str(binary), "--home", str(home), "--json", *arguments], env)
    except ValueError as exc:
        return failure(str(exc))
    except subprocess.TimeoutExpired:
        return failure("Paseo CLI timed out. Inspect agents before retrying; a submitted task may still be running.",
                       uncertain=action_name in MUTATIONS)
    except OSError:
        return failure("Could not invoke Paseo CLI")
    if result.returncode:
        # No raw stderr: daemon diagnostics may contain connection credentials or task text.
        return failure("Paseo CLI returned an error. Check status/agents before retrying a mutation.",
                       uncertain=action_name in MUTATIONS, exit_code=result.returncode)
    if action == "logs":
        return json.dumps({"status": "ok", "action": action, "agent_id": args["agent_id"],
                           "text": result.stdout[-20_000:], "truncated": len(result.stdout) > 20_000,
                           "exit_code": 0}, ensure_ascii=False)
    try:
        data = json.loads(result.stdout)
        if not isinstance(data, (dict, list)) or (isinstance(data, dict) and "error" in data):
            raise ValueError("Unexpected Paseo response")
    except ValueError:
        return failure("Paseo returned no valid receipt. Inspect agents before retrying a mutation.",
                       uncertain=action_name in MUTATIONS, exit_code=0)
    if action in {"run", "send"}:
        if (not isinstance(data, dict) or not isinstance(data.get("agentId"), str) or
                not isinstance(data.get("status"), str)):
            return failure("Paseo returned no agent receipt. Check agents before retrying.", uncertain=True)
        if action == "send" and data["agentId"] != args["agent_id"]:
            return failure("Paseo receipt identified a different agent; inspect agents before retrying.", uncertain=True)
    if action == "status":
        if not isinstance(data, dict):
            return failure("Invalid Paseo status response")
        data = {key: data[key] for key in ("pid", "workerPid", "startedAt", "daemonVersion", "providers", "desktopManaged") if key in data}
    if len(json.dumps(data, ensure_ascii=False)) > 80_000:
        return failure("Paseo response exceeded the output limit; inspect one agent instead",
                       uncertain=action_name in MUTATIONS)
    return json.dumps({"status": "ok", "action": action, "data": data, "exit_code": 0,
                       **({"task_completed": False} if action in {"run", "send"} else {})}, ensure_ascii=False)
