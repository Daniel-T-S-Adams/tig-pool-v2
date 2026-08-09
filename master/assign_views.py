"""Background assign views for scalable get-batches.

Hot-path /get-batches must stay memory-only (TIG-style). Smart InnoPool
policy (proof artifacts, finish-root, adaptive caps, capability meta, auth)
is refreshed here on a timer / after slave_manager.run(), then read atomically
by the poll handler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Set, Tuple


ArtifactKey = Tuple[str, str, int]  # slave, benchmark_id, batch_idx


@dataclass
class AssignViews:
    """Immutable-ish snapshot consumed by get-batches."""

    updated_ms: int = 0
    # (slave, benchmark_id, batch_idx) that may take proof work
    proof_artifacts: Set[ArtifactKey] = field(default_factory=set)
    # slave -> benchmark_ids that should still take roots while proof-priority
    finish_root_by_slave: Dict[str, Set[str]] = field(default_factory=dict)
    # slaves with unfinished proof obligations
    awaiting_proofs: Set[str] = field(default_factory=set)
    # slave -> adaptive concurrent cap (already clamped to route semantics)
    adaptive_caps: Dict[str, int] = field(default_factory=dict)
    # capability scheduler
    cap_enabled: bool = False
    cap_views: dict = field(default_factory=dict)
    job_meta: Dict[str, dict] = field(default_factory=dict)
    # warmed auth allow-list (pool- members)
    authorized_slaves: Set[str] = field(default_factory=set)

    def may_take_proof(self, slave: str, benchmark_id: str, batch_idx: int) -> bool:
        return (str(slave), str(benchmark_id), int(batch_idx)) in self.proof_artifacts

    def finish_roots(self, slave: str) -> Set[str]:
        return set(self.finish_root_by_slave.get(str(slave)) or set())
