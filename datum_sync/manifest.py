"""Workspace manifest: parse, validate, and coerce published parameters.

The manifest is the workspace's interface contract. It is validated once at
registration and the validated form is stored in the database, so an on-disk
edit cannot silently change a published interface.

Stored form is normalised, not the original bytes: defaults are materialised
(`required: false`, `choices: null`), so the runtime reads a manifest with no
implicit values left to re-derive.
"""
from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator


class ManifestError(Exception):
    """Manifest is structurally invalid."""


class ParameterError(Exception):
    """Submitted parameters do not satisfy the manifest."""


class ParameterType(str, Enum):
    STRING = "STRING"
    INTEGER = "INTEGER"
    FLOAT = "FLOAT"
    BOOLEAN = "BOOLEAN"
    FILE = "FILE"
    LOOKUP_CHOICE = "LOOKUP_CHOICE"


SERVICES = frozenset(
    {"job_submitter", "data_streaming", "data_download", "data_upload"}
)

_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


class Parameter(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    type: ParameterType
    required: bool = False
    default: Any = None
    choices: list[str] | None = None
    description: str | None = None
    group: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "Parameter":
        if self.type is ParameterType.LOOKUP_CHOICE:
            if not self.choices:
                raise ValueError(f"{self.name}: LOOKUP_CHOICE requires 'choices'")
            if self.default is not None and self.default not in self.choices:
                raise ValueError(
                    f"{self.name}: default {self.default!r} is not one of {self.choices}"
                )
        elif self.choices is not None:
            raise ValueError(f"{self.name}: 'choices' is only valid for LOOKUP_CHOICE")

        # required + default is ambiguous: the default means it can never be
        # missing, so "required" would never fire. Reject rather than guess.
        if self.required and self.default is not None:
            raise ValueError(
                f"{self.name}: a required parameter cannot also declare a default"
            )
        return self


class Output(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    type: str
    primary: bool = False


class ConnectionRef(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    access: Literal["read", "write"] = "read"


class Manifest(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    description: str | None = None
    version: str = "0.1.0"
    parameters: list[Parameter] = Field(default_factory=list)
    outputs: list[Output] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    connections: list[ConnectionRef] = Field(default_factory=list)
    timeout_seconds: int = 300

    # Opt in to the publish gate running `python main.py --smoke`. Declared here
    # rather than detected, because there is no way to ask a script whether it
    # supports a flag except by running it -- and a workspace with no --smoke
    # handler does not fail, it runs its normal path with an argument it ignores.
    # That would make "the smoke test passed" mean "the workspace ran for real",
    # which for anything with side effects is the opposite of a safe check.
    #
    # Default false so the gate stays free unless a workspace asks for it.
    smoke_test: bool = False

    @model_validator(mode="after")
    def _check(self) -> "Manifest":
        self._reject_duplicates([p.name for p in self.parameters], "parameter")
        self._reject_duplicates([o.name for o in self.outputs], "output")
        self._reject_duplicates([c.name for c in self.connections], "connection")

        unknown = sorted(set(self.services) - SERVICES)
        if unknown:
            raise ValueError(
                f"unknown service(s) {unknown}; valid: {sorted(SERVICES)}"
            )

        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        # The download and streaming services return one body, so the manifest
        # has to say which output that is. With a single output it is implied.
        primaries = [o.name for o in self.outputs if o.primary]
        if len(primaries) > 1:
            raise ValueError(f"only one output may be primary, got {primaries}")
        if len(self.outputs) > 1 and not primaries:
            raise ValueError(
                "with more than one output, exactly one must be marked primary"
            )
        return self

    @staticmethod
    def _reject_duplicates(names: list[str], label: str) -> None:
        seen, dupes = set(), set()
        for n in names:
            (dupes if n in seen else seen).add(n)
        if dupes:
            raise ValueError(f"duplicate {label} name(s): {sorted(dupes)}")

    @property
    def primary_output(self) -> Output | None:
        for o in self.outputs:
            if o.primary:
                return o
        return self.outputs[0] if len(self.outputs) == 1 else None


def load_manifest(path: Path, expected_name: str | None = None) -> Manifest:
    """Parse and validate a manifest.json.

    expected_name, when given, must equal the manifest's declared name -- the
    directory name is what callers address, so the two disagreeing means the
    workspace would be callable under a name its own manifest does not use.
    """
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise ManifestError(f"{path}: no manifest.json") from None
    except json.JSONDecodeError as e:
        raise ManifestError(f"{path}: invalid JSON - {e}") from None

    try:
        manifest = Manifest.model_validate(raw)
    except ValidationError as e:
        raise ManifestError(f"{path}: {_format_errors(e)}") from None

    if expected_name is not None and manifest.name != expected_name:
        raise ManifestError(
            f"{path}: manifest name {manifest.name!r} does not match "
            f"directory name {expected_name!r}"
        )
    return manifest


def _format_errors(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "(root)"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def _coerce(param: Parameter, value: Any) -> Any:
    t = param.type

    if t is ParameterType.BOOLEAN:
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        raise ParameterError(f"{param.name}: {value!r} is not a boolean")

    if t is ParameterType.INTEGER:
        # bool is a subclass of int; accepting it would turn True into 1.
        if isinstance(value, bool):
            raise ParameterError(f"{param.name}: expected an integer, got a boolean")
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            raise ParameterError(f"{param.name}: {value!r} is not an integer") from None

    if t is ParameterType.FLOAT:
        if isinstance(value, bool):
            raise ParameterError(f"{param.name}: expected a number, got a boolean")
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            raise ParameterError(f"{param.name}: {value!r} is not a number") from None

    if t is ParameterType.LOOKUP_CHOICE:
        s = str(value)
        if s not in (param.choices or []):
            raise ParameterError(
                f"{param.name}: {s!r} is not one of {param.choices}"
            )
        return s

    # STRING and FILE both arrive as text (FILE carries an upload id).
    return str(value)


def validate_params(manifest: Manifest, raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and coerce submitted parameters against the manifest.

    Unknown parameter names are an error, not a silent no-op. A mistyped
    parameter that is quietly dropped produces a job that "succeeds" having
    done nothing, which is far more expensive to diagnose than a rejection.
    """
    declared = {p.name: p for p in manifest.parameters}

    unknown = sorted(set(raw) - set(declared))
    if unknown:
        raise ParameterError(
            f"unknown parameter(s) {unknown}; declared: {sorted(declared)}"
        )

    result: dict[str, Any] = {}
    missing: list[str] = []

    for name, param in declared.items():
        if name in raw and raw[name] is not None:
            result[name] = _coerce(param, raw[name])
        elif param.default is not None:
            result[name] = param.default
        elif param.required:
            missing.append(name)

    if missing:
        raise ParameterError(f"missing required parameter(s): {sorted(missing)}")

    return result
