"""Argument guards: what a federated call may name (spec/agent-auth-plane/05 §4).

Pure. A guard lives on the connection's config, names upstream tools by glob,
and for each names the argument values that must fall inside a field of the
principal's federation block. Nothing here touches the database or the
network except through the `resolver` a caller may pass for `resolve:
drive_folder`, and that is a coroutine the caller owns.

The grammar is deliberately small (D-16): a restricted JSONPath (`$`,
`.name`, `[*]`, `[n]`), `join` for `owner/repo`, `const` for a server with
one target. A guard that cannot find its argument denies (§4.3): a missing
repo is not "any repo".
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from datum_sync import grants

MAX_REGEX_LEN = 1024
MAX_ARGS_BYTES = 256 * 1024

GRANT_FIELDS = ("repos", "hosts", "folders", "paths.read", "paths.write", "commands")
REQUIRE_KEYS = ("write", "share", "tier", "approval")
RESOLVERS = ("drive_folder",)


class GuardError(ValueError):
    """A guard definition that cannot be compiled. Refused at save."""


@dataclass
class Denied(Exception):
    """A call refused by a guard. `field` names the grant field that refused."""
    reason: str
    guard: str
    field: str | None = None

    def __str__(self) -> str:
        return self.reason


@dataclass
class Check:
    grant: str
    value: dict[str, Any]
    optional: bool = False


@dataclass
class Guard:
    tools: list[str]
    checks: list[Check] = field(default_factory=list)
    requires: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return ",".join(self.tools)

    def matches(self, upstream_name: str) -> bool:
        return any(fnmatch.fnmatchcase(upstream_name, g) for g in self.tools)


# -- compilation -------------------------------------------------------------------

_PATH_TOKEN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_-]*)|\[(\*|\d+)\]")


def parse_path(path: str) -> list[Any]:
    """`$.files[*].path` -> ['files', '*', 'path']. Raises GuardError."""
    if not isinstance(path, str) or not path.startswith("$"):
        raise GuardError(f"a path starts with '$': {path!r}")
    rest, out, pos = path[1:], [], 0
    while pos < len(rest):
        m = _PATH_TOKEN.match(rest, pos)
        if m is None:
            raise GuardError(f"cannot parse path {path!r} at {rest[pos:]!r}")
        out.append(m.group(1) if m.group(1) is not None else
                   ("*" if m.group(2) == "*" else int(m.group(2))))
        pos = m.end()
    return out


def _compile_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GuardError("a check's value is an object: {path}, {join, sep} or {const}")
    forms = [k for k in ("path", "join", "const") if k in value]
    if len(forms) != 1:
        raise GuardError("a check's value has exactly one of path, join, const")
    out = dict(value)
    if "path" in value:
        out["_path"] = parse_path(value["path"])
    elif "join" in value:
        if not isinstance(value["join"], list) or not value["join"]:
            raise GuardError("join takes a non-empty list of paths")
        out["_paths"] = [parse_path(p) for p in value["join"]]
        out.setdefault("sep", "/")
    elif not isinstance(value["const"], str):
        raise GuardError("const is a string")
    if "resolve" in value and value["resolve"] not in RESOLVERS:
        raise GuardError(f"unknown resolver {value['resolve']!r}")
    return out


def compile_guards(raw: Any) -> list[Guard]:
    """The connection's `config.guards`, checked. Raises GuardError."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise GuardError("guards is a list")
    out: list[Guard] = []
    for i, g in enumerate(raw):
        if not isinstance(g, dict):
            raise GuardError(f"guards[{i}] is an object")
        tools = g.get("tools")
        if not isinstance(tools, list) or not tools or not all(isinstance(t, str) and t for t in tools):
            raise GuardError(f"guards[{i}].tools is a non-empty list of tool globs")
        checks = []
        for j, c in enumerate(g.get("checks") or []):
            if not isinstance(c, dict) or c.get("grant") not in GRANT_FIELDS:
                raise GuardError(f"guards[{i}].checks[{j}].grant is one of {', '.join(GRANT_FIELDS)}")
            checks.append(Check(grant=c["grant"], value=_compile_value(c.get("value")),
                                optional=bool(c.get("optional", False))))
        requires = g.get("requires") or {}
        if not isinstance(requires, dict) or set(requires) - set(REQUIRE_KEYS):
            raise GuardError(f"guards[{i}].requires keys are {', '.join(REQUIRE_KEYS)}")
        if "tier" in requires and not (isinstance(requires["tier"], int) and 1 <= requires["tier"] <= 5):
            raise GuardError(f"guards[{i}].requires.tier is 1-5")
        if "approval" in requires and not (isinstance(requires["approval"], str) and requires["approval"]):
            raise GuardError(f"guards[{i}].requires.approval is a label")
        out.append(Guard(tools=list(tools), checks=checks, requires=dict(requires)))
    return out


def compile_resource_guards(raw: Any) -> list[dict[str, Any]]:
    """`config.resource_guards`: [{uri: glob, grant: field, value: {regex_group}}]."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise GuardError("resource_guards is a list")
    out = []
    for i, g in enumerate(raw):
        if not isinstance(g, dict) or not isinstance(g.get("uri"), str):
            raise GuardError(f"resource_guards[{i}].uri is a glob")
        if g.get("grant") not in GRANT_FIELDS:
            raise GuardError(f"resource_guards[{i}].grant is one of {', '.join(GRANT_FIELDS)}")
        value = g.get("value") or {}
        rx = value.get("regex_group")
        if not isinstance(rx, str) or len(rx) > MAX_REGEX_LEN:
            raise GuardError(f"resource_guards[{i}].value.regex_group is a regex under {MAX_REGEX_LEN} bytes")
        try:
            compiled = re.compile(rx)
        except re.error as exc:
            raise GuardError(f"resource_guards[{i}].value.regex_group: {exc}") from None
        out.append({"uri": g["uri"], "grant": g["grant"], "_rx": compiled})
    return out


def compile_commands(block: dict[str, Any]) -> tuple[list[re.Pattern[str]], list[re.Pattern[str]]]:
    """The block's command regexes, compiled once per evaluation."""
    spec = block.get("commands") or {}
    out = []
    for key in ("deny", "allow"):
        pats = []
        for p in spec.get(key) or []:
            if not isinstance(p, str) or len(p) > MAX_REGEX_LEN:
                raise GuardError(f"commands.{key}: a regex under {MAX_REGEX_LEN} bytes")
            try:
                pats.append(re.compile(p))
            except re.error as exc:
                raise GuardError(f"commands.{key} {p!r}: {exc}") from None
        out.append(pats)
    return out[0], out[1]


# -- extraction ------------------------------------------------------------------------


class _Missing(Exception):
    pass


def walk(obj: Any, path: list[Any]) -> list[Any]:
    """Every value the path resolves to. `[*]` and list values fan out."""
    if not path:
        return [obj]
    head, rest = path[0], path[1:]
    if head == "*":
        if not isinstance(obj, list):
            raise _Missing()
        out = []
        for item in obj:
            out.extend(walk(item, rest))
        return out
    if isinstance(head, int):
        if not isinstance(obj, list) or head >= len(obj):
            raise _Missing()
        return walk(obj[head], rest)
    if not isinstance(obj, dict) or head not in obj:
        raise _Missing()
    return walk(obj[head], rest)


def _scalars(values: list[Any]) -> list[str]:
    out = []
    for v in values:
        if isinstance(v, list):
            out.extend(_scalars(v))
        elif isinstance(v, str):
            out.append(v)
        else:
            # A number, a bool, an object, null: not a target (§4.3).
            raise _Missing()
    return out


def extract(value: dict[str, Any], args: dict[str, Any]) -> list[str] | None:
    """The guarded values, or None when the path resolves to nothing."""
    if "const" in value:
        return [value["const"]]
    if "_path" in value:
        try:
            found = walk(args, value["_path"])
        except _Missing:
            return None
        if not found:
            return None
        try:
            return _scalars(found)
        except _Missing:
            raise Denied("a guarded argument is not a string", "", None)
    parts = []
    for path in value["_paths"]:
        try:
            found = walk(args, path)
        except _Missing:
            return None
        if len(found) != 1 or not isinstance(found[0], str):
            # `join` needs one scalar per path (FED-017): a list of owners
            # joined to one repo is not a repository name.
            raise Denied("join: each path must resolve to one string", "", None)
        parts.append(found[0])
    return [value.get("sep", "/").join(parts)]


# -- evaluation -----------------------------------------------------------------------


def requires_met(requires: dict[str, Any], block: dict[str, Any], tier: int) -> str | None:
    """Why the requirement is not met, or None. `approval` is not a refusal."""
    if requires.get("write") and not block.get("write"):
        return "the block is read-only"
    if requires.get("share") and not block.get("share"):
        return "the block does not allow sharing"
    if "tier" in requires and tier < requires["tier"]:
        return f"tier {requires['tier']} is required"
    return None


def command_allowed(value: str, deny: list[re.Pattern[str]], allow: list[re.Pattern[str]]) -> bool:
    """Deny regexes first (FED-008), then allow, else deny."""
    if any(p.search(value) for p in deny):
        return False
    return any(p.search(value) for p in allow)


def _field(block: dict[str, Any], name: str) -> list[str]:
    obj: Any = block
    for part in name.split("."):
        obj = (obj or {}).get(part) if isinstance(obj, dict) else None
    return list(obj or []) if isinstance(obj, list) else []


Resolver = Callable[[str], Awaitable[list[str]]]


async def evaluate(
    guards: list[Guard], block: dict[str, Any], tier: int, args: dict[str, Any],
    resolver: Resolver | None = None,
) -> tuple[dict[str, list[str]], list[str]]:
    """Apply every guard. Returns (guarded values by field, approval labels).

    Raises Denied on the first refusal. All matching guards apply (§4.1).
    """
    guarded: dict[str, list[str]] = {}
    approvals: list[str] = []
    deny_rx, allow_rx = compile_commands(block)
    for g in guards:
        why = requires_met(g.requires, block, tier)
        if why:
            raise Denied(f"{why} ({g.label})", g.label, None)
        if g.requires.get("approval"):
            approvals.append(g.requires["approval"])
        for c in g.checks:
            try:
                values = extract(c.value, args)
            except Denied as exc:
                raise Denied(f"{exc.reason} ({g.label})", g.label, c.grant) from None
            if values is None:
                if c.optional:
                    continue
                raise Denied(f"argument for {c.grant} is missing ({g.label})", g.label, c.grant)
            if c.value.get("resolve") == "drive_folder":
                resolved = []
                for v in values:
                    try:
                        chain = await resolver(v) if resolver else None
                    except Exception:  # noqa: BLE001 - any failure to resolve is a deny
                        chain = None
                    if not chain:
                        raise Denied(f"could not resolve {v!r} to a folder ({g.label})", g.label, c.grant)
                    resolved.append(chain)
                # The file passes when any ancestor is a granted folder.
                for v, chain in zip(values, resolved):
                    if not any(grants.matches(p, f) for f in chain for p in _field(block, "folders")):
                        raise Denied(f"{v!r} is outside the granted folders ({g.label})", g.label, c.grant)
                guarded.setdefault(c.grant, []).extend(values)
                continue
            if c.grant == "commands":
                for v in values:
                    if not command_allowed(v, deny_rx, allow_rx):
                        raise Denied(f"command not allowed ({g.label})", g.label, c.grant)
            else:
                patterns = _field(block, c.grant)
                for v in values:
                    if not any(grants.matches(p, v) for p in patterns):
                        raise Denied(f"{v!r} is outside {c.grant} ({g.label})", g.label, c.grant)
            guarded.setdefault(c.grant, []).extend(values)
    return guarded, approvals


def visible(guards: list[Guard], block: dict[str, Any], tier: int) -> bool:
    """Whether a tool with these guards is worth listing for this principal."""
    return all(requires_met(g.requires, block, tier) is None for g in guards)


def tool_allowed(block: dict[str, Any], upstream_name: str) -> bool:
    """The block's tools allow/deny. Deny wins; no allow match is not listed."""
    spec = block.get("tools") or {}
    if any(fnmatch.fnmatchcase(upstream_name, d) for d in spec.get("deny") or []):
        return False
    return any(fnmatch.fnmatchcase(upstream_name, a) for a in spec.get("allow") or [])


def resource_value(resource_guards: list[dict[str, Any]], uri: str) -> tuple[str, str] | None:
    """(grant field, value) for the first resource guard whose glob matches."""
    for g in resource_guards:
        if fnmatch.fnmatchcase(uri, g["uri"]):
            m = g["_rx"].search(uri)
            if m is None:
                return g["grant"], ""
            return g["grant"], (m.group(1) if m.groups() else m.group(0))
    return None


# -- validation against the cached schema ---------------------------------------------

_PROPERTY_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def schema_warnings(guards: list[Guard], tools: dict[str, dict[str, Any]]) -> list[str]:
    """Guards naming arguments no tool declares, and names a client would drop.

    `tools` maps upstream tool names to input schemas. A warning is a UI
    message and an audit row; the guard still denies at call time.
    """
    out: list[str] = []
    for g in guards:
        matched = [n for n in tools if g.matches(n)]
        if not matched:
            out.append(f"guard {g.label!r} matches no tool on the upstream")
            continue
        for c in g.checks:
            paths = [c.value["_path"]] if "_path" in c.value else c.value.get("_paths", [])
            for p in paths:
                if not p:
                    continue
                first = p[0]
                for n in matched:
                    props = (tools[n] or {}).get("properties") or {}
                    if isinstance(first, str) and first not in props:
                        out.append(f"guard {g.label!r}: tool {n!r} declares no argument {first!r}")
    for n, schema in tools.items():
        for prop in (schema or {}).get("properties") or {}:
            if not _PROPERTY_NAME.match(str(prop)):
                out.append(f"tool {n!r}: property {prop!r} is outside [A-Za-z0-9_.-]; Claude Code drops the tool")
    return out
