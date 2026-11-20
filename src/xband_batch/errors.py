"""Structured exceptions for batch orchestration."""


class BatchError(Exception):
    """Base error for xband_batch."""


class ConfigError(BatchError):
    """Raised when configuration is missing, malformed, or violates policy."""

    def __init__(self, source: object, field: str, message: str):
        self.source = str(source)
        self.field = field
        self.message = message
        super().__init__(f"{self.source} [{self.field}]: {self.message}")


class PathUnavailableError(BatchError):
    """Raised when configured data root env var or directory is missing."""


class PreflightBlockedError(BatchError):
    """Raised when preflight detects unresolved antennas or ambiguities."""

    def __init__(self, pending_count: int, report_path: object):
        self.pending_count = pending_count
        self.report_path = str(report_path)
        super().__init__(
            f"preflight blocked: {pending_count} pending item(s); inspect {self.report_path}"
        )


class AdapterNotImplementedError(NotImplementedError, BatchError):
    """Raised by scaffold adapter stubs until wired to a concrete backend."""
