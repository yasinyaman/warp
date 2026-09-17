"""Arrow IPC support for the streaming export endpoint.

``pyarrow`` is an optional dependency (``pip install warp-engine[arrow]``);
this module imports it lazily so the rest of the HTTP adapter works without it.
"""

import importlib.util


def arrow_available() -> bool:
    """Whether ``pyarrow`` can be imported in this environment."""
    return importlib.util.find_spec("pyarrow") is not None
