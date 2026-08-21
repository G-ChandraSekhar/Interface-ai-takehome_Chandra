"""
Capability registry.

This is the literal realization of the brief's own thesis: "the artifact
becomes a reusable capability... this is how the AI agent invokes it in
production." An artifact JSON file on disk is a review-friendly document;
this module turns it into the shape an actual AI agent's tool-calling loop
expects (the same OpenAI function-schema shape used in src/discovery/tools.py),
generated automatically FROM the artifact's own typed input_params /
output_schema -- never hand-duplicated, so the schema an agent sees can
never drift from what the artifact actually declares.
"""

from __future__ import annotations

from pathlib import Path

from src.artifact.schema import Artifact
from src.artifact.store import load_artifact

_TYPE_MAP = {"str": "string", "int": "integer", "decimal": "string"}


def discover_artifacts(artifacts_dir):
    """Loads every artifact JSON file in artifacts_dir. Does not deduplicate
    by artifact_id -- multiple versions of the same capability may coexist,
    and the catalog exposes each one explicitly by id@version."""
    artifacts = []
    if not artifacts_dir.exists():
        return artifacts
    for path in sorted(artifacts_dir.glob("*.json")):
        try:
            artifacts.append(load_artifact(path))
        except Exception:
            continue
    return artifacts


def artifact_to_tool_schema(artifact):
    """Converts an artifact into an OpenAI-style function tool schema, plus
    a few catalog-specific fields (artifact_id, version, output_schema) an
    agent or operator console can use that aren't part of the strict
    OpenAI tool-schema shape."""
    properties = {
        name: {
            "type": _TYPE_MAP.get(spec.type, "string"),
            "description": spec.description or name,
        }
        for name, spec in artifact.input_params.items()
    }
    required = [name for name, spec in artifact.input_params.items() if spec.required]

    return {
        "type": "function",
        "function": {
            "name": artifact.artifact_id,
            # NEVER artifact.goal. The goal is the instruction given to the
            # model at recording time -- it names the member the recording
            # used, the values it set, and instructions like "set all three
            # fields even if a value already appears". Published as a tool
            # description, a model reads it as guidance for the CALLER and
            # obeys it: asked to change a phone number, it demanded an e-mail
            # and an address the schema said were optional.
            "description": artifact.description or artifact.name,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
        "artifact_id": artifact.artifact_id,
        "version": artifact.version,
        "approved": artifact.approved,
        "output_schema": {
            name: _TYPE_MAP.get(spec.type, "string") for name, spec in artifact.output_schema.items()
        },
    }
