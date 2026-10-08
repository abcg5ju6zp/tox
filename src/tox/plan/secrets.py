from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tox.util.redact import _SECRET_ENV_VAR_REGEX

if TYPE_CHECKING:
    from tox.util.json_types import JsonValue

_SECRET_PREFIX = "__TOX_PLAN_SECRET_"  # ruff: ignore[hardcoded-password-string]
_SECRET_SUFFIX = "__"  # ruff: ignore[hardcoded-password-string]


@dataclass(frozen=True)
class SecretBinding:
    identifier: str
    name: str
    source: str
    value: str
    origin: str = "environment"
    env_name: str | None = None
    file_path: str | None = None
    relative_path: str | None = None

    @property
    def length(self) -> int:
        return len(self.value)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.value.encode()).hexdigest()

    def metadata(self) -> dict[str, JsonValue]:
        metadata: dict[str, JsonValue] = {
            "id": self.identifier,
            "name": self.name,
            "source": self.source,
            "origin": self.origin,
            "length": self.length,
            "sha256": self.sha256,
        }
        if self.env_name is not None:
            metadata["env"] = self.env_name
        if self.file_path is not None:
            file_metadata: dict[str, JsonValue] = {"path": self.file_path}
            if self.relative_path is not None:
                file_metadata["relative_path"] = self.relative_path
            metadata["file"] = file_metadata
        return metadata


def is_secret_name(name: str) -> bool:
    if name in {"PWD", "OLDPWD"}:
        return False
    return bool(_SECRET_ENV_VAR_REGEX.match(name))


def binding_identifier(source: str, name: str) -> str:
    digest = hashlib.sha256(f"{source}\0{name}".encode()).hexdigest()[:16]
    safe_source = re.sub(r"[^A-Za-z0-9_.-]", "_", source)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    return f"{safe_source}:{safe_name}:{digest}"


def secret_binding(  # ruff:ignore[too-many-arguments]
    source: str,
    name: str,
    value: str,
    *,
    origin: str = "environment",
    env_name: str | None = None,
    file_path: str | None = None,
    relative_path: str | None = None,
) -> SecretBinding:
    return SecretBinding(
        binding_identifier(source, name),
        name=name,
        source=source,
        value=value,
        origin=origin,
        env_name=env_name,
        file_path=file_path,
        relative_path=relative_path,
    )


def placeholder(identifier: str) -> str:
    return f"{_SECRET_PREFIX}{identifier}{_SECRET_SUFFIX}"


def redact_plan(plan: dict[str, JsonValue], bindings: Mapping[str, SecretBinding]) -> None:
    redacted = _redact(plan, bindings)
    if isinstance(redacted, dict):
        plan.clear()
        plan.update(redacted)


def bind_plan_secrets(plan: JsonValue, values: Mapping[str, str]) -> JsonValue:
    return _bind(plan, values)


def binding_metadata(bindings: Mapping[str, SecretBinding]) -> list[JsonValue]:
    return [bindings[identifier].metadata() for identifier in sorted(bindings)]


def iter_secret_placeholders(value: JsonValue) -> Iterator[str]:
    if isinstance(value, str):
        yield from re.findall(rf"{re.escape(_SECRET_PREFIX)}(.+?){re.escape(_SECRET_SUFFIX)}", value)
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from iter_secret_placeholders(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from iter_secret_placeholders(child)


def values_match(binding: SecretBinding, value: str | None) -> bool:
    return value is not None and value == binding.value


def _redact(value: JsonValue, bindings: Mapping[str, SecretBinding]) -> JsonValue:
    if isinstance(value, str):
        result = value
        for binding in sorted(bindings.values(), key=lambda item: item.length, reverse=True):
            result = result.replace(binding.value, placeholder(binding.identifier))
        return result
    if isinstance(value, Mapping):
        return {str(key): _redact(child, bindings) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_redact(child, bindings) for child in value]
    return value


def _bind(value: JsonValue, values: Mapping[str, str]) -> JsonValue:
    if isinstance(value, str):
        result = value
        for identifier, secret in values.items():
            result = result.replace(placeholder(identifier), secret)
        return result
    if isinstance(value, Mapping):
        return {str(key): _bind(child, values) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_bind(child, values) for child in value]
    return value
