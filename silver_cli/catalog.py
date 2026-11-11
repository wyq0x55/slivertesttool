"""Operation catalogue shipped with the CLI.

``catalog.json`` is generated from the platform's route source by
``scripts/export_cli_catalog.py`` and packaged next to this module, so the CLI
runs on machines without the platform source tree (including from a zipapp).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from functools import lru_cache
from importlib import resources
from pathlib import Path


CATALOG_PATH = Path(__file__).resolve().with_name("catalog.json")


@dataclass(frozen=True)
class Operation:
    name: str
    method: str
    path: str
    summary: str
    path_params: dict[str, str]
    query_params: list[str]
    body_fields: list[str]
    form_fields: list[str]
    file_fields: list[str]
    response_kind: str
    mutating: bool

    def to_record(self):
        return asdict(self)

    def to_dict(self):
        result = asdict(self)
        result["field_hints_only"] = True
        result["validation"] = "Server validation and permissions remain authoritative. Nested/conditional fields may not be listed."
        return result


@lru_cache(maxsize=1)
def operations():
    text = resources.files(__package__).joinpath("catalog.json").read_text(encoding="utf-8")
    names = {field.name for field in fields(Operation)}
    return [Operation(**{key: row[key] for key in names}) for row in json.loads(text)["operations"]]


def get_operation(name):
    for operation in operations():
        if operation.name == name:
            return operation
    raise ValueError("Unknown operation; use list to discover supported operations.")
