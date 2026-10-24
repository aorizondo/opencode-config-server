"""Loads the corporate configuration document and injects server-side values.

What IT writes in `config/opencode.jsonc` is served almost verbatim. Two
conveniences are resolved here so the document stays free of deployment detail:

    {server_url}     -> the public base URL
    {session_token}  -> `{env:<session env>}`, which the harness substitutes
                        locally with the employee's own token

`{env:NAME}` and `{file:path}` are left untouched: the harness resolves both on
the employee's machine (packages/core/src/config/variable.ts).
"""

from __future__ import annotations

import re
from typing import Any

from . import jsonc
from .settings import Settings

#: Rejected outright: the harness would read these from the employee's disk and
#: they are a classic way to turn a config feed into an exfiltration channel.
FORBIDDEN_FILE_REFERENCES = ("{file:~/.ssh", "{file:/etc/shadow", "{file:~/.aws", "{file:~/.gnupg")


class ConfigError(RuntimeError):
    """The corporate document is missing or cannot be served."""


def _substitute(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for token, replacement in replacements.items():
            value = value.replace(token, replacement)
        return value
    if isinstance(value, list):
        return [_substitute(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, replacements) for key, item in value.items()}
    return value


def parse(text: str, *, source: str = "the configuration") -> dict[str, Any]:
    """The document IT wrote, comments removed; refuses what must never be served."""
    for marker in FORBIDDEN_FILE_REFERENCES:
        if marker in text:
            raise ConfigError(f"refusing to serve a configuration containing {marker!r} ({source})")
    try:
        document = jsonc.loads(text)
    except ValueError as error:
        raise ConfigError(f"{source} is not valid JSON/JSONC: {error}") from error
    if not isinstance(document, dict):
        raise ConfigError(f"{source} must contain a JSON object")
    return document


def render(document: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """What an employee's harness receives for `document`."""
    rendered = _substitute(
        document,
        {
            "{server_url}": settings.base_url,
            "{session_token}": f"{{env:{settings.session_env}}}",
        },
    )
    rendered.setdefault("$schema", "https://opencode.ai/config.json")
    return rendered


def load(settings: Settings) -> dict[str, Any]:
    path = settings.opencode_config_file
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"corporate configuration not readable: {path}") from error
    return render(parse(text, source=str(path)), settings)


# -- shape checks -------------------------------------------------------------
# A light mirror of Config.Info (packages/schema/src/config.ts, v2.1.1) for the
# sections the admin panel edits. The harness ignores unknown keys and rejects a
# document whose known keys have the wrong shape, so the first are warnings here
# and the second errors.

KNOWN_KEYS = frozenset(
    {
        "$schema", "shell", "model", "default_agent", "update", "share", "enterprise", "username",
        "permissions", "policies", "agents", "snapshots", "watcher", "formatter", "lsp", "media",
        "tool_output", "mcp", "compaction", "skills", "commands", "instructions", "references",
        "websearch", "plugins", "worktree", "warming", "providers", "experimental",
    }
)
_MODEL = re.compile(r"^[^/#]+/[^#]+(?:#[^#]+)?$")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
#: Header or variable names whose value should come from `{env:…}`, never inline.
_SECRET_NAME = re.compile(r"(authorization|token|secret|password|api[-_]?key)", re.IGNORECASE)


class _Problems:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def type(self, value: Any, kind: type | tuple[type, ...], where: str, label: str) -> bool:
        if isinstance(value, bool) and kind in (int, (int, float)):
            ok = False
        else:
            ok = isinstance(value, kind)
        if not ok:
            self.errors.append(f"{where} must be {label}")
        return ok

    def choice(self, value: Any, options: tuple[str, ...], where: str) -> None:
        if value not in options:
            self.errors.append(f"{where} must be one of {', '.join(options)}")

    def strings(self, value: Any, where: str) -> None:
        if self.type(value, list, where, "a list of strings") and any(not isinstance(item, str) for item in value):
            self.errors.append(f"{where} must be a list of strings")

    def string_map(self, value: Any, where: str) -> None:
        if not self.type(value, dict, where, "an object of strings"):
            return
        for key, item in value.items():
            if not isinstance(item, str):
                self.errors.append(f"{where}.{key} must be a string")
            elif _SECRET_NAME.search(key) and "{env:" not in item and "{session_token}" not in item:
                self.warnings.append(
                    f"{where}.{key} holds a literal value: every employee can read this document, "
                    "use {env:NAME} instead"
                )

    def model(self, value: Any, where: str) -> None:
        if isinstance(value, str):
            if not _MODEL.match(value):
                self.errors.append(f"{where} must look like provider/model (optionally #variant)")
        elif isinstance(value, dict):
            if not isinstance(value.get("providerID"), str) or not isinstance(value.get("model"), str):
                self.errors.append(f"{where} needs string providerID and model")
        else:
            self.errors.append(f"{where} must be a provider/model string")

    def ruleset(self, value: Any, where: str, *, actions: tuple[str, ...] | None = None,
                effects: tuple[str, ...] = ("allow", "deny", "ask")) -> None:
        if not self.type(value, list, where, "a list of rules"):
            return
        for index, rule in enumerate(value):
            at = f"{where}[{index}]"
            if not self.type(rule, dict, at, "an object with action, resource and effect"):
                continue
            if actions:
                self.choice(rule.get("action"), actions, f"{at}.action")
            else:
                self.type(rule.get("action"), str, f"{at}.action", "a string")
            self.type(rule.get("resource"), str, f"{at}.resource", "a string")
            self.choice(rule.get("effect"), effects, f"{at}.effect")


def check(document: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(errors, warnings) for a document before it is saved."""
    p = _Problems()
    for key in document:
        if key not in KNOWN_KEYS:
            p.warnings.append(f"{key!r} is not an OpenCode v2 setting; the harness ignores it")

    for key in ("shell", "default_agent", "username"):
        if key in document:
            p.type(document[key], str, key, "a string")
    if "model" in document:
        p.model(document["model"], "model")
    if "update" in document:
        p.choice(document["update"], ("disable", "notify", "auto"), "update")
    if "share" in document:
        p.choice(document["share"], ("manual", "auto", "disabled"), "share")
    if "snapshots" in document:
        p.type(document["snapshots"], bool, "snapshots", "true or false")
    for key in ("skills", "instructions"):
        if key in document:
            p.strings(document[key], key)
    if "permissions" in document:
        p.ruleset(document["permissions"], "permissions")
    if "policies" in document:
        p.ruleset(document["policies"], "policies", actions=("provider.use", "tool.use", "integration.use"),
                  effects=("allow", "deny"))

    agents = document.get("agents")
    if agents is not None and p.type(agents, dict, "agents", "an object keyed by agent name"):
        for name, agent in agents.items():
            at = f"agents.{name}"
            if not p.type(agent, dict, at, "an object"):
                continue
            for field in ("system", "description"):
                if field in agent:
                    p.type(agent[field], str, f"{at}.{field}", "a string")
            for field in ("hidden", "disabled"):
                if field in agent:
                    p.type(agent[field], bool, f"{at}.{field}", "true or false")
            if "mode" in agent:
                p.choice(agent["mode"], ("subagent", "primary", "all"), f"{at}.mode")
            if "model" in agent:
                p.model(agent["model"], f"{at}.model")
            if "color" in agent and not (isinstance(agent["color"], str) and _COLOR.match(agent["color"])):
                p.errors.append(f"{at}.color must be a #rrggbb colour")
            if "steps" in agent and p.type(agent["steps"], int, f"{at}.steps", "a positive integer"):
                if agent["steps"] < 1:
                    p.errors.append(f"{at}.steps must be a positive integer")
            if "permissions" in agent:
                p.ruleset(agent["permissions"], f"{at}.permissions")

    commands = document.get("commands")
    if commands is not None and p.type(commands, dict, "commands", "an object keyed by command name"):
        for name, command in commands.items():
            at = f"commands.{name}"
            if not p.type(command, dict, at, "an object"):
                continue
            if not isinstance(command.get("template"), str) or not command["template"].strip():
                p.errors.append(f"{at}.template is required")
            for field in ("description", "agent"):
                if field in command:
                    p.type(command[field], str, f"{at}.{field}", "a string")
            for field in ("subagent", "subtask"):
                if field in command:
                    p.type(command[field], bool, f"{at}.{field}", "true or false")
            if "model" in command:
                p.model(command["model"], f"{at}.model")

    mcp = document.get("mcp")
    if mcp is not None and p.type(mcp, dict, "mcp", "an object with servers"):
        servers = mcp.get("servers", {})
        if p.type(servers, dict, "mcp.servers", "an object keyed by server name"):
            for name, server in servers.items():
                at = f"mcp.servers.{name}"
                if not p.type(server, dict, at, "an object"):
                    continue
                kind = server.get("type")
                if kind == "local":
                    command = server.get("command")
                    p.strings(command, f"{at}.command")
                    if isinstance(command, list) and not command:
                        p.errors.append(f"{at}.command needs at least the program to run")
                    if isinstance(command, list) and command and all(isinstance(arg, str) for arg in command):
                        # OpenCode runs command[0] with command[1:] as arguments, no shell
                        # (packages/core/src/mcp/client.ts), so a joined command line
                        # is one program name with spaces in it.
                        if len(command) == 1 and " " in command[0].strip():
                            p.warnings.append(
                                f"{at}.command is a single string with spaces: OpenCode runs it as one program "
                                "name; put the program and each argument in its own list item"
                            )
                        if any(arg.startswith("~") for arg in command):
                            p.warnings.append(
                                f"{at}.command uses ~, which is not expanded (no shell): use an absolute "
                                "path, or a variable defined per user such as {env:PROJECTS_DIR}"
                            )
                    if "environment" in server:
                        p.string_map(server["environment"], f"{at}.environment")
                elif kind == "remote":
                    if not isinstance(server.get("url"), str) or not server["url"]:
                        p.errors.append(f"{at}.url is required")
                    if "headers" in server:
                        p.string_map(server["headers"], f"{at}.headers")
                else:
                    p.errors.append(f"{at}.type must be local or remote")
                if "disabled" in server:
                    p.type(server["disabled"], bool, f"{at}.disabled", "true or false")
        for key in mcp:
            if key not in ("servers", "timeout"):
                p.warnings.append(
                    f"mcp.{key} is not read by OpenCode v2: servers go under mcp.servers"
                )

    plugins = document.get("plugins")
    if plugins is not None and p.type(plugins, list, "plugins", "a list"):
        for index, plugin in enumerate(plugins):
            at = f"plugins[{index}]"
            spec = plugin.get("package") if isinstance(plugin, dict) else plugin
            if not isinstance(spec, str) or not spec:
                p.errors.append(f"{at} must be a package spec or {{package, options}}")
                continue
            if isinstance(plugin, dict) and "options" in plugin:
                p.type(plugin["options"], dict, f"{at}.options", "an object")
            if spec.startswith(("./", "../")):
                p.warnings.append(
                    f"{at} is a relative path: in a remote document it resolves against each "
                    "employee's working directory; use an npm spec or an absolute path"
                )
    providers = document.get("providers")
    if providers is not None and p.type(providers, dict, "providers", "an object keyed by provider id"):
        for name, provider in providers.items():
            at = f"providers.{name}"
            if not p.type(provider, dict, at, "an object"):
                continue
            settings = provider.get("settings")
            if settings is not None and p.type(settings, dict, f"{at}.settings", "an object"):
                for key in ("apiKey", "authToken", "accessToken", "bearerToken"):
                    value = settings.get(key)
                    if isinstance(value, str) and value and "{env:" not in value and "{file:" not in value:
                        p.warnings.append(
                            f"{at}.settings.{key} holds a literal key: every employee can read this document; "
                            "write {env:NAME} and define NAME under Variables"
                        )
            if "headers" in provider:
                p.string_map(provider["headers"], f"{at}.headers")
    return p.errors, p.warnings
