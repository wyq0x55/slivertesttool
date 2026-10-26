"""Validate an authoritative, finalized generation receipt before dispatch."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def read_receipt(path: Path, output_root: Path) -> dict:
    document = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(document, dict) or document.get("contract") != "ut-agent.generated-unit/1"
            or not isinstance(document.get("provenance"), dict) or not document["provenance"]
            or not isinstance(document.get("unit"), dict)):
        raise ValueError("Invalid generated-unit receipt contract")
    unit, artifacts = document["unit"], document.get("artifacts")
    if not isinstance(unit.get("function"), str) or not isinstance(artifacts, dict):
        raise TypeError("Missing generated-unit identity/artifacts")
    directory = Path(unit.get("output_dir") or "")
    if not directory.is_absolute() or not directory.resolve().is_relative_to(output_root.resolve()):
        raise ValueError("Generated unit escapes its batch output directory")
    required = {"testcsv", "ir_json", "intent_manifest", "intent_summary"} if unit.get("testcsv") else set()
    if not required <= artifacts.keys():
        raise ValueError("Ready CSV is missing finalized typed artifacts")
    for key, artifact in artifacts.items():
        if key not in {"testcsv", "ir_json", "intent_manifest", "intent_summary", "stub"} or not isinstance(artifact, dict):
            raise ValueError("Invalid generated-unit artifact")
        location = Path(artifact.get("path") or "")
        if (not location.is_absolute() or not location.resolve().is_relative_to(directory.resolve())
                or not location.is_file() or location.is_symlink()):
            raise ValueError("Generated artifact escapes its completed unit")
        if key == "intent_summary":
            expected = Path(unit.get("intent_manifest") or "").with_name("test-intents-summary.json")
        else:
            expected = Path(unit.get(key) or "")
        if location != expected or hashlib.sha256(location.read_bytes()).hexdigest() != artifact.get("sha256"):
            raise ValueError("Generated artifact path/SHA does not match its receipt")
    return document
