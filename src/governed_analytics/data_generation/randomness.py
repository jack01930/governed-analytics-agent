"""Namespace-isolated pseudorandom streams for data generation."""

from hashlib import sha256

import numpy as np
from numpy.random import Generator


def named_rng(seed: int, namespace: str) -> Generator:
    """Create a repeatable generator isolated from all other namespaces."""
    digest = sha256(f"{seed}:{namespace}".encode()).digest()
    namespace_seed = int.from_bytes(digest[:8], byteorder="big", signed=False)
    return np.random.default_rng(namespace_seed)
