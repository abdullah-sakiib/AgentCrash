from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator


class FaultMode(StrEnum):
    TIMEOUT_BEFORE_COMMIT = "timeout_before_commit"
    TIMEOUT_AFTER_COMMIT = "timeout_after_commit"


class SimulatedTimeout(Exception):
    """Raised to simulate a lost response (HTTP 504 to the caller)."""


class SimulatedHttpError(Exception):
    """Raised to simulate an HTTP error before commit (e.g. HTTP 503)."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class FaultType(StrEnum):
    """What failure to inject."""

    NONE = "none"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"


class FaultTarget(StrEnum):
    """Which business operation the fault applies to."""

    PAYMENT_CREATE = "payment.create"


class FaultPoint(StrEnum):
    """Lifecycle point within the target operation where the fault fires."""

    BEFORE_COMMIT = "before_commit"
    AFTER_COMMIT = "after_commit"


_SUPPORTED_FAULTS = frozenset(
    {
        (
            FaultType.TIMEOUT,
            FaultTarget.PAYMENT_CREATE,
            FaultPoint.BEFORE_COMMIT,
        ),
        (
            FaultType.TIMEOUT,
            FaultTarget.PAYMENT_CREATE,
            FaultPoint.AFTER_COMMIT,
        ),
        (
            FaultType.HTTP_ERROR,
            FaultTarget.PAYMENT_CREATE,
            FaultPoint.BEFORE_COMMIT,
        ),
    }
)


class FaultConfig(BaseModel):
    """Explicit, extensible fault-injection configuration.

    Omitting the configuration (or ``fault_type="none"``) preserves normal
    behavior. Unknown combinations are rejected instead of silently ignored.
    Timeout faults always surface as HTTP 504; HTTP-error faults surface as
    their configured status code (currently only 503). Neither status code
    is negotiable per fault instance beyond the validated configuration.
    """

    model_config = ConfigDict(extra="forbid")

    fault_type: FaultType = FaultType.NONE
    target: FaultTarget = FaultTarget.PAYMENT_CREATE
    point: FaultPoint = FaultPoint.AFTER_COMMIT
    status_code: int | None = None

    @model_validator(mode="after")
    def check_supported(self) -> Self:
        if self.fault_type == FaultType.NONE:
            if (
                self.target != FaultTarget.PAYMENT_CREATE
                or self.point != FaultPoint.AFTER_COMMIT
                or self.status_code is not None
            ):
                raise ValueError(
                    "target/point/status_code must be left at defaults "
                    "when fault_type is 'none'"
                )
            return self
        if self.fault_type == FaultType.TIMEOUT:
            if self.status_code is not None:
                raise ValueError(
                    "status_code is not configurable for timeout faults; "
                    "timeouts always return HTTP 504"
                )
        if self.fault_type == FaultType.HTTP_ERROR:
            if self.status_code != 503:
                raise ValueError(
                    "http_error faults require status_code 503 "
                    "(the only supported HTTP error)"
                )
        if (self.fault_type, self.target, self.point) not in _SUPPORTED_FAULTS:
            raise ValueError(
                "Unsupported fault combination: "
                f"{self.fault_type.value}/{self.target.value}/{self.point.value}"
            )
        return self


def structured_fault(fault_mode: str | None) -> dict[str, str] | None:
    """Map a legacy ``fault_mode`` string to its structured equivalent."""
    if fault_mode is None:
        return None
    if fault_mode == FaultMode.TIMEOUT_BEFORE_COMMIT.value:
        point = FaultPoint.BEFORE_COMMIT.value
    elif fault_mode == FaultMode.TIMEOUT_AFTER_COMMIT.value:
        point = FaultPoint.AFTER_COMMIT.value
    else:
        raise ValueError(f"Unknown fault_mode: {fault_mode!r}")
    return {
        "fault_type": FaultType.TIMEOUT.value,
        "target": FaultTarget.PAYMENT_CREATE.value,
        "point": point,
    }


def inject_before_commit(fault: FaultConfig | None) -> None:
    """Fail a payment request before any business state is committed."""
    if fault is None or fault.fault_type == FaultType.NONE:
        return
    if fault.fault_type == FaultType.TIMEOUT and fault.point == FaultPoint.BEFORE_COMMIT:
        raise SimulatedTimeout("Payment not committed; the request timed out")
    if fault.fault_type == FaultType.HTTP_ERROR and fault.point == FaultPoint.BEFORE_COMMIT:
        assert fault.status_code is not None  # guaranteed by FaultConfig validation
        raise SimulatedHttpError(
            fault.status_code, "Payment not committed; the service is unavailable"
        )


def inject_after_commit(fault: FaultConfig | None, *, was_created: bool) -> None:
    """Fail a payment request after durable state is committed."""
    if fault is None or fault.fault_type == FaultType.NONE:
        return
    if (
        fault.fault_type == FaultType.TIMEOUT
        and fault.point == FaultPoint.AFTER_COMMIT
        and was_created
    ):
        raise SimulatedTimeout("Payment committed, but the response timed out")

