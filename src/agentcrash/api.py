from collections.abc import Iterator

from fastapi import Depends, FastAPI, HTTPException, Request, status
from sqlalchemy.orm import Session

from agentcrash import models  # noqa: F401 - registers metadata
from agentcrash.database import Database
from agentcrash.evaluator import evaluate_persisted_state
from agentcrash.faults import (
    SimulatedHttpError,
    SimulatedTimeout,
    inject_after_commit,
    inject_before_commit,
)
from agentcrash.schemas import (
    EvaluationRead,
    InventoryRead,
    InventoryWrite,
    OrderCreate,
    OrderRead,
    PaymentCreate,
    PaymentRead,
)
from agentcrash.services import (
    ConflictError,
    NotFoundError,
    charge_order,
    create_order,
    fulfill_order,
    get_orders_by_intent,
    get_payment_by_key,
    set_inventory,
)


def get_session(request: Request) -> Iterator[Session]:
    yield from request.app.state.database.sessions()


def create_app(database_url: str = "sqlite:///./agentcrash.db") -> FastAPI:
    app = FastAPI(title="AgentCrash Simulator", version="0.1.0")
    app.state.database = Database(database_url)
    app.state.database.create_all()

    # Imported here (not at module top): ui pulls experiment modules that
    # import create_app themselves, so this must run after api is loaded.
    from agentcrash.ui import router as ui_router

    app.include_router(ui_router)

    @app.post(
        "/inventory",
        response_model=InventoryRead,
        status_code=status.HTTP_201_CREATED,
    )
    def write_inventory(
        body: InventoryWrite, session: Session = Depends(get_session)
    ) -> InventoryRead:
        return InventoryRead.model_validate(
            set_inventory(
                session, item=body.item, quantity_on_hand=body.quantity_on_hand
            )
        )

    @app.post(
        "/orders", response_model=OrderRead, status_code=status.HTTP_201_CREATED
    )
    def post_order(
        body: OrderCreate, session: Session = Depends(get_session)
    ) -> OrderRead:
        try:
            order = create_order(
                session,
                item=body.item,
                quantity=body.quantity,
                unit_price=body.unit_price,
                intent_key=body.intent_key,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return OrderRead.model_validate(order)

    @app.get("/orders/by-intent-key/{intent_key}", response_model=OrderRead)
    def order_by_intent(
        intent_key: str, session: Session = Depends(get_session)
    ) -> OrderRead:
        orders = get_orders_by_intent(session, intent_key)
        if not orders:
            raise HTTPException(
                status_code=404, detail=f"No order for intent '{intent_key}'"
            )
        return OrderRead.model_validate(orders[0])

    @app.post("/orders/{order_id}/payments", response_model=PaymentRead)
    def post_payment(
        order_id: int,
        body: PaymentCreate,
        session: Session = Depends(get_session),
    ) -> PaymentRead:
        try:
            fault = body.resolved_fault()
            inject_before_commit(fault)
            result = charge_order(
                session, order_id=order_id, idempotency_key=body.idempotency_key
            )
            inject_after_commit(fault, was_created=result.was_created)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SimulatedTimeout as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except SimulatedHttpError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail=str(exc)
            ) from exc
        payment_response = PaymentRead.model_validate(result.payment)
        return payment_response.model_copy(
            update={"replayed": not result.was_created}
        )

    @app.get(
        "/payments/by-idempotency-key/{idempotency_key}",
        response_model=PaymentRead,
    )
    def payment_status(
        idempotency_key: str, session: Session = Depends(get_session)
    ) -> PaymentRead:
        try:
            payment = get_payment_by_key(session, idempotency_key)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return PaymentRead.model_validate(payment)

    @app.post("/orders/{order_id}/fulfill", response_model=OrderRead)
    def fulfill(
        order_id: int, session: Session = Depends(get_session)
    ) -> OrderRead:
        try:
            order = fulfill_order(session, order_id=order_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return OrderRead.model_validate(order)

    @app.get("/evaluation", response_model=EvaluationRead)
    def evaluation(session: Session = Depends(get_session)) -> EvaluationRead:
        result = evaluate_persisted_state(session)
        return EvaluationRead.model_validate(result, from_attributes=True)

    # Mounted last so simulator, /api, docs, and OpenAPI routes all take
    # precedence; the SPA fallback inside the mount excludes those paths.
    from agentcrash.static_ui import mount_frontend

    mount_frontend(app)

    return app
