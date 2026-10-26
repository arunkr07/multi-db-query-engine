class MultiDBQueryEngineError(Exception):
    """Base class for SDK errors."""


class ConfigurationError(MultiDBQueryEngineError):
    pass


class QueryPlanningError(MultiDBQueryEngineError):
    pass


class DatabaseExecutionError(MultiDBQueryEngineError):
    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


class SecurityViolationError(MultiDBQueryEngineError):
    pass


class ApprovalError(MultiDBQueryEngineError):
    pass


class UnsupportedOperationError(MultiDBQueryEngineError):
    pass
