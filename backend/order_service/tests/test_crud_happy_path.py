# backend/order_service/tests/test_crud_happy_path.py
from fastapi.testclient import TestClient
from typing import Tuple, Optional

# Import the FastAPI app and the DB dependency for override
try:
    # Week08 skeleton usually exposes these here:
    from app.main import app, get_db
except Exception:  # fallback if path differs a bit
    from app import main as _main
    app = _main.app
    get_db = _main.get_db  # type: ignore[attr-defined]

# SQLAlchemy in-memory session for the app during tests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import models  # Week08 skeleton has models.Base

# ---- dependency override: use in-memory sqlite so routes truly execute ----
engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
models.Base.metadata.create_all(bind=engine)

def _override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = _override_get_db  # type: ignore[arg-type]

client = TestClient(app)

# Some repos use "/orders", others use "/api/orders"
def _detect_orders_base(c: TestClient) -> str:
    for base in ("/api/orders", "/orders"):
        r = c.get(base)
        if r.status_code in (200, 204, 405, 422):
            return base
    return "/orders"

def _mk_order_payload() -> dict:
    """
    A permissive payload that usually matches Week08 order_service schemas.
    If your schema differs, add/remove fields accordingly.
    """
    return {
        "customer_id": 1,
        "status": "PENDING",
        # many Week08 templates accept items as a list with product_id & quantity
        "items": [{"product_id": 111, "quantity": 1}],
        # optional fields that are often present; harmless if ignored
        "note": "test",
        "total": 0.0,
    }

def _extract_order_id(obj: dict) -> Optional[int]:
    """Different templates name the id differently; try common keys."""
    for k in ("id", "order_id", "orderId"):
        if k in obj and isinstance(obj[k], int):
            return obj[k]
    return None

def test_create_get_update_delete_order_happy_path():
    base = _detect_orders_base(client)

    # CREATE
    r = client.post(f"{base}/", json=_mk_order_payload())
    # If your create returns 201/200 with the entity
    assert r.status_code in (200, 201)
    data = r.json()
    oid = _extract_order_id(data)
    assert oid is not None, f"could not find order id in response: {data}"

    # GET (should be 200)
    r = client.get(f"{base}/{oid}")
    assert r.status_code == 200

    # UPDATE (flip status; many skeletons accept partial update or full put)
    r = client.put(f"{base}/{oid}", json={"status": "CANCELLED"})
    assert r.status_code in (200, 204)

    # DELETE (should be 200/204)
    r = client.delete(f"{base}/{oid}")
    assert r.status_code in (200, 204)

    # GET after delete should 404 (exercises another branch)
    r = client.get(f"{base}/{oid}")
    assert r.status_code == 404
