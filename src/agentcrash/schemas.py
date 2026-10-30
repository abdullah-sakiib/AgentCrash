from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentcrash.faults import FaultConfig, structured_fault


Money = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
ItemName = Annotated[str, Field(min_length=1, max_length=100)]
IdempotencyKey = Annotated[str, Field(min_length=1, max_length=100)]


class InventoryWrite(BaseModel):
    item: ItemName
    quantity_on_hand: Annotated[int, Field(ge=0)]


class InventoryRead(InventoryWrite):
    model_config = ConfigDict(from_attributes=True)


class OrderCreate(BaseModel):
    item: ItemName
    quantity: Annotated[int, Field(gt=0)]
    unit_price: Money
    intent_key: Annotated[str, Field(min_length=1, max_length=100)] | None = None


class OrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    item: str
    quantity: int
    unit_price: Decimal
    intent_key: str | None = None
    total: Decimal
    status: str
    created_at: datetime


class PaymentCreate(BaseModel):
    idempotency_key: IdempotencyKey
    fault_mode: Literal["timeout_before_commit", "timeout_after_commit"] | None = None
    fault: FaultConfig | None = None

    @model_validator(mode="after")
    def check_single_fault_source(self) -> Self:
        if self.fault is not None and self.fault_mode is not None:
            raise ValueError(
                "Provide only one of 'fault' or legacy 'fault_mode'"
            )
        return self

    def resolved_fault(self) -> FaultConfig | None:
        """Effective structured fault configuration for injection.

        Prefers the explicit structured ``fault``; otherwise maps the
        legacy ``fault_mode`` string. ``None`` means normal behavior.
        """
        if self.fault is not None:
            return self.fault
        if self.fault_mode is None:
            return None
        legacy = structured_fault(self.fault_mode)
        assert legacy is not None  # fault_mode is not None here
        return FaultConfig.model_validate(legacy)


class PaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    idempotency_key: str
    amount: Decimal
    status: str
    created_at: datetime
    replayed: bool = False


class ViolationRead(BaseModel):
    code: str
    message: str


class EvaluationRead(BaseModel):
    valid: bool
    orders_checked: int
    payments_checked: int
    violations: list[ViolationRead]

