class InfrastructureError(Exception):
    pass


class OperationError(InfrastructureError):
    pass


class OperationTimeout(OperationError):
    pass


class StepTimeout(OperationTimeout):
    pass


class GroupTimeout(OperationTimeout):
    pass


class CleanupTimeout(OperationTimeout):
    pass


class RestoreError(InfrastructureError):
    pass


class CleanupError(InfrastructureError):
    pass


class RecoveryError(InfrastructureError):
    pass
