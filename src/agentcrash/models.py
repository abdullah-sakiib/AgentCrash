from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agentcrash.database import Base


class OrderStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    FULFILLED = "fulfilled"


class PaymentStatus(StrEnum):
    SUCCEEDED = "succeeded"


class InventoryItem(Base):
    __tablename__ = "inventory_items"
    __table_args__ = (
        CheckConstraint("quantity_on_hand >= 0", name="ck_inventory_nonnegative"),
    )

    item: Mapped[str] = mapped_column(String(100), primary_key=True)
    quantity_on_hand: Mapped[int] = mapped_column(Integer, nullable=False)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_quantity_positive"),
        CheckConstraint("unit_price > 0", name="ck_order_price_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item: Mapped[str] = mapped_column(ForeignKey("inventory_items.item"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # Customer intent key for idempotent order submission. Nullable so rows
    # created before this column existed are ignored by the duplicate-intent
    # invariant; non-unique so duplicate submissions remain representable.
    intent_key: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=OrderStatus.PENDING_PAYMENT.value
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    payments: Mapped[list["PaymentTransaction"]] = relationship(back_populates="order")

    @property
    def total(self) -> Decimal:
        return self.unit_price * self.quantity


class PaymentTransaction(Base):
    __tablename__ = "payment_transactions"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payment_amount_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=PaymentStatus.SUCCEEDED.value
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    order: Mapped[Order] = relationship(back_populates="payments")

