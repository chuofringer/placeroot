"""Structured exceptions shared across the query-layer modules.

The dataset param customizes the label in SchemaDegraded messages
("dataset" vs "transportation dataset"). overture.py and routing.py
re-export these as module-level names (aliases in overture.py; a subclass
pinning the label in routing.py) so except-clauses can catch them from
either module.
"""


class UpstreamUnavailable(Exception):
    """A remote scan failed after DuckDB's built-in retries were exhausted.

    detail is a short, agent-safe message — never a raw stack trace.
    """

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class SchemaDegraded(Exception):
    """An essential column is missing from the active dataset.

    dataset labels the message ("required columns missing from {dataset}:
    ...") — overture.py's callers (places/divisions/buildings, all sharing
    one message) leave it at the default "dataset"; routing.py's subclass
    pins it to "transportation dataset" to keep its original wording.
    """

    def __init__(self, missing: list[str], dataset: str = "dataset"):
        detail = f"required columns missing from {dataset}: {', '.join(missing)}"
        super().__init__(detail)
        self.detail = detail
        self.missing = missing


class AmbiguousArea(Exception):
    """A free-text area name matched several equally-ranked divisions.

    Raised by geocode.resolve_area rather than silently picking one, so the
    caller can hand the agent the actual candidates to choose between.
    candidates is a list of {"division_id", "name", "admin_context"}.
    """

    def __init__(self, area: str, candidates: list[dict]):
        detail = (
            f"{area!r} matches {len(candidates)} equally-ranked divisions; "
            "pass one of the listed division_id values instead"
        )
        super().__init__(detail)
        self.detail = detail
        self.area = area
        self.candidates = candidates
