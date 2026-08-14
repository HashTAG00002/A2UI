"""CompilerObservationView — the ONLY observation type L4 accepts.

Pure layer-internal DTO (handoff 04: C defines it, C never sees B's
``SubstrateObservation`` concrete type). Everything on it is content a real
user could see on the rendered screen (GG red line §0):

- ``VisibleRegion`` — one observed surface (window/page/app view): its
  user-visible name, the visible text (a11y / rendered text), an optional
  screenshot data-URL, and a ``structure_fingerprint`` (a cheap structural
  hash the substrate converter computes — fast/slow path routing input).
- ``HandleEvidence`` — TaskVM-owned handle knowledge from a previous
  compilation: the handle id (ours, not an app id), the visible label it was
  grounded on, its visible context, the value read last time, and the
  deterministic ``value_pattern`` that re-reads the value from visible text.

The composition/runtime layer converts substrate observations into this DTO
deterministically (Ethernet frame → IP packet): any DB primary key, internal
operator or non-visible DOM attribute must be dropped BEFORE construction —
constructing this DTO from leaked inputs is the producer's bug, not ours.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from taskvm.domain.errors import ValidationError
from taskvm.domain.state import SurfaceHandle


@dataclass(frozen=True)
class VisibleRegion:
    """One visible surface at observation time."""

    surface_label: str          # what a user would call this window/page/app
    visible_text: str           # rendered / a11y text — the primary input
    structure_fingerprint: str = ""
    screenshot_data_url: str | None = None

    def __post_init__(self) -> None:
        if not self.surface_label:
            raise ValidationError("VisibleRegion.surface_label must be non-empty")


@dataclass(frozen=True)
class HandleEvidence:
    """Prior compilation knowledge grounding one task variable."""

    handle: SurfaceHandle
    semantic_key: str
    surface_label: str          # the region the label was seen on
    visible_label: str          # the on-screen string that names the quantity
    visible_context: str = ""
    value_pattern: str = ""     # regex, exactly one capture group, or ""
    last_value: object = None

    def __post_init__(self) -> None:
        if not self.semantic_key:
            raise ValidationError(
                "HandleEvidence.semantic_key must be non-empty")
        if not self.visible_label:
            raise ValidationError(
                "HandleEvidence.visible_label must be non-empty")


@dataclass(frozen=True)
class CompilerObservationView:
    """The immutable observation contract between L1/L2 and L4.

    ``revision``: monotonic observation revision (from the converter). An
    empty ``regions`` tuple is legal (e.g. all surfaces closed) — the
    compiler then honestly reports an empty world.
    """

    revision: int
    regions: tuple[VisibleRegion, ...] = ()
    handle_cache: tuple[HandleEvidence, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "regions", tuple(self.regions))
        object.__setattr__(self, "handle_cache", tuple(self.handle_cache))
        if self.revision < 0:
            raise ValidationError("revision must be >= 0")
        labels = [r.surface_label for r in self.regions]
        if len(set(labels)) != len(labels):
            raise ValidationError(
                f"duplicate surface_label in observation view: {labels}")

    def region(self, surface_label: str) -> VisibleRegion | None:
        for r in self.regions:
            if r.surface_label == surface_label:
                return r
        return None

    def visible_digest(self) -> str:
        """Deterministic text digest for prompts (region order preserved)."""
        parts = [f"## Surface: {r.surface_label}\n{r.visible_text.rstrip()}"
                 for r in self.regions]
        return "\n\n".join(parts)

    def fingerprints(self) -> dict[str, str]:
        return {r.surface_label: r.structure_fingerprint for r in self.regions}

    def handle(self, semantic_key: str) -> HandleEvidence | None:
        for h in self.handle_cache:
            if h.semantic_key == semantic_key:
                return h
        return None
