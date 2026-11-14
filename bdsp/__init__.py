"""Dataset Plazz: an agent-facing search platform over opt-in datasets.

Three contracts, one loop:
    declaration (contract.py) -> proof (proof/router.py) -> invocation (gate/api.py)

The `bdsp` package name is kept for import stability; the product name is
"Dataset Plazz" and the CLI is `datasetplazz`.
"""
from .api import Platform
from .contract import ContractError, describe, iter_sources, validate
from .errors import REASONS, fail, ok
from .gate import Gate
from .index import build
from .proof import build_proof
from .router import route

__version__ = "0.4.1"

__all__ = [
    "Platform",
    "Gate",
    "build",
    "build_proof",
    "route",
    "iter_sources",
    "describe",
    "validate",
    "ContractError",
    "REASONS",
    "fail",
    "ok",
]
