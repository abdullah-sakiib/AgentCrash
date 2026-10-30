from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agentcrash.models import Order, OrderStatus, PaymentStatus, PaymentTransaction


@dataclass(frozen=True)
class Violation:
    code: str
    message: str


@dataclass(frozen=True)
class Evaluation:
    valid: bool
    orders_checked: int
    payments_checked: int
    violations: list[Violation]


def evaluate_persisted_state(session: Session) -> Evaluation:
    orders = list(session.scalars(select(Order).order_by(Order.id)))
    orders_by_id = {order.id: order for order in orders}
    payments = list(
        session.scalars(select(PaymentTransaction).order_by(PaymentTransaction.id))
    )
    violations: list[Violation] = []

    duplicate_keys = session.execute(
        select(PaymentTransaction.idempotency_key, func.count(PaymentTransaction.id))
        .where(PaymentTransaction.status == PaymentStatus.SUCCEEDED.value)
        .group_by(PaymentTransaction.idempotency_key)
        .having(func.count(PaymentTransaction.id) > 1)
    ).all()
    for key, count in duplicate_keys:
        violations.append(
            Violation(
                code="duplicate_successful_charge",
                message=f"Payment intent '{key}' has {count} successful charges",
            )
        )

    payments_by_order: dict[int, list[PaymentTransaction]] = {}
    for payment in payments:
        payments_by_order.setdefault(payment.order_id, []).append(payment)
        order = orders_by_id.get(payment.order_id)
        if order is not None and payment.amount != order.total:
            violations.append(
                Violation(
                    code="payment_amount_mismatch",
                    message=(
                        f"Payment {payment.id} amount {payment.amount} does not match "
                        f"order {order.id} total {order.total}"
                    ),
                )
            )

    orders_by_intent: dict[str, list[Order]] = {}
    for order in orders:
        if order.intent_key:
            orders_by_intent.setdefault(order.intent_key, []).append(order)
    for intent_key, intent_orders in orders_by_intent.items():
        if len(intent_orders) > 1:
            order_ids = [order.id for order in intent_orders]
            reserved = sum(order.quantity for order in intent_orders)
            violations.append(
                Violation(
                    code="duplicate_orders_for_intent",
                    message=(
                        f"Intent '{intent_key}' has {len(intent_orders)} orders; "
                        f"order_ids={order_ids}, "
                        f"reserved_quantity={reserved}"
                    ),
                )
            )

    for order in orders:
        successful = [
            payment
            for payment in payments_by_order.get(order.id, [])
            if payment.status == PaymentStatus.SUCCEEDED.value
        ]
        if order.status == OrderStatus.FULFILLED.value and not successful:
            violations.append(
                Violation(
                    code="fulfilled_without_payment",
                    message=(
                        f"Order {order.id} is fulfilled with no successful charge; "
                        f"inventory for '{order.item}' was reserved "
                        f"(quantity={order.quantity}) without payment"
                    ),
                )
            )
        if len(successful) > 1:
            payment_ids = [payment.id for payment in successful]
            idempotency_keys = [payment.idempotency_key for payment in successful]
            violations.append(
                Violation(
                    code="multiple_successful_charges_for_order",
                    message=(
                        f"Order {order.id} has {len(successful)} successful charges; "
                        f"payment_ids={payment_ids}, "
                        f"idempotency_keys={idempotency_keys}"
                    ),
                )
            )
        if order.status == OrderStatus.PAID.value and not successful:
            violations.append(
                Violation(
                    code="paid_order_charge_count",
                    message=f"Paid order {order.id} has no successful charge",
                )
            )
        if order.status == OrderStatus.PENDING_PAYMENT.value and successful:
            violations.append(
                Violation(
                    code="pending_order_has_charge",
                    message=f"Pending order {order.id} has a successful charge",
                )
            )

    return Evaluation(
        valid=not violations,
        orders_checked=len(orders),
        payments_checked=len(payments),
        violations=violations,
    )
