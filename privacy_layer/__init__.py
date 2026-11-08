"""IncidentVoice privacy layer: scrubber, egress leak gate, local incident search.

    from privacy_layer import scrub, log_egress, search_incidents
"""
import importlib

_EXPORTS = {
    "scrub": "scrubber", "scrub_obj": "scrubber", "TYPES": "scrubber",
    "log_egress": "egress", "EgressBlocked": "egress", "find_residual": "egress", "read_log": "egress", "summarize": "egress", "describe": "egress",
    "search_incidents": "search",
}
__all__ = list(_EXPORTS)


def __getattr__(name):  # lazy, so `python -m privacy_layer.<module>` runs cleanly
    if name in _EXPORTS:
        return getattr(importlib.import_module(f".{_EXPORTS[name]}", __name__), name)
    raise AttributeError(name)
