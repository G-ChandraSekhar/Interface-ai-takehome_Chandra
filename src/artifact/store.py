"""
Artifact storage.

Artifacts are stored as plain JSON files, one per version, named
`<artifact_id>@<version>.json`. No database -- at this scale a flat file per
reviewed capability is simpler, is trivially diffable in a PR/code review
(which is exactly how these should be reviewed), and needs no
infrastructure the exercise doesn't call for.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.artifact.schema import Artifact


def artifact_filename(artifact_id: str, version: int) -> str:
    return artifact_id + "@" + str(version) + ".json"


def save_artifact(artifact: Artifact, artifacts_dir: Path) -> Path:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    path = artifacts_dir / artifact_filename(artifact.artifact_id, artifact.version)
    with open(path, "w") as f:
        f.write(artifact.model_dump_json(indent=2))
    return path


def load_artifact(path: Path) -> Artifact:
    with open(path, "r") as f:
        data = json.load(f)
    return Artifact.model_validate(data)


def load_artifact_by_id(artifact_id: str, version: int, artifacts_dir: Path) -> Artifact:
    path = artifacts_dir / artifact_filename(artifact_id, version)
    return load_artifact(path)
