"""Structured exceptions shared across the query-layer modules (issue #40).

overture.py and routing.py used to define their own, textually identical
UpstreamUnavailable and near-identical SchemaDegraded (differing only in
the dataset label in the message — "dataset" vs "transportation dataset";
the dataset param preserves both). Both modules still expose these as
module-level names (aliases in overture.py; a subclass pinning the label
in routing.py) so every existing except-clause keeps working unchanged.
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
