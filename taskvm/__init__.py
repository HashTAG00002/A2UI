"""TaskVM — compile live state of multiple existing applications into an editable,
executable, verifiable task interface.

VM five properties (docs/A2UI_开工大纲_v0_心智模型对齐版.md §3):
bottom-up live projection, bidirectional executability, substrate
independence, governance over autonomy, independent verification with
honest reversibility. The runtime-visible verifier judges completion from
fresh visible observation (never hidden canonical state); the
binding-generating model never self-judges.

Layered plane: domain → kernel → architect → runtime / projection over the
substrate port (see docs/contracts/*.md).
"""
__version__ = "0.2.0"
