# SPDX-FileCopyrightText: Copyright (c) 2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Build exact entities from L2-grounded failure evidence."""

from __future__ import annotations

from ..identity import build_affected_entity
from ..models import AffectedEntity, AffectedEntityKind


def build_grounded_affected_entity(
    *,
    artifact_path: str | None,
    evidence_line: int | None,
) -> AffectedEntity | None:
    """Build an exact entity from source-grounded current-failure evidence."""

    if artifact_path is None:
        return None
    normalized_path = artifact_path.rstrip("/") or "/"
    return build_affected_entity(
        AffectedEntityKind.ARTIFACT,
        normalized_path,
        evidence_line=evidence_line,
    )
