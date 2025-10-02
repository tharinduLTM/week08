# backend/order_service/tests/test_openapi_driven_orders.py
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app, get_db  # type: ignore
from app.db import Base as DbBase
from app import models  # noqa: F401 - ensure models register with metadata


# --- shared in-memory DB so all sessions see the same data --------------------
engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
DbBase.metadata.create_all(bind=engine)


def _override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = _override_get_db  # type: ignore[arg-type]
client = TestClient(app)


# ---------------------------- helpers -----------------------------------------
def _find_orders_paths(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Return all openapi paths that include 'orders'."""
    paths = spec.get("paths", {})
    return {k: v for k, v in paths.items() if "orders" in k}


def _resolve(ref: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a $ref like '#/components/schemas/OrderCreate'."""
    assert ref.startswith("#/"), f"Unsupported $ref: {ref}"
    node: Any = spec
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _example_from_schema(s: Dict[str, Any], spec: Dict[str, Any]) -> Any:
    """Build a minimal valid example payload from an OpenAPI schema node."""
    if "$ref" in s:
        return _example_from_schema(_resolve(s["$ref"], spec), spec)

    # AnyOf/OneOf – just pick the first
    for key in ("anyOf", "oneOf", "allOf"):
        if key in s and isinstance(s[key], list) and s[key]:
            return _example_from_schema(s[key][0], spec)

    t = s.get("type")
    fmt = s.get("format")

    if "enum" in s and isinstance(s["enum"], list) and s["enum"]:
        return s["enum"][0]

    if t == "string":
        if fmt in ("date-time", "datetime"):
            return "2025-01-01T00:00:00"
        if fmt == "date":
            return "2025-01-01"
        return "x"
    if t == "integer":
        return 1
    if t == "number":
        return 1.0
    if t == "boolean":
        return True
    if t == "array":
        # minimal array: one example element if we can infer items
        items = s.get("items", {"type": "string"})
        return [_example_from_schema(items, spec)]
    if t == "object" or ("properties" in s):
        props = s.get("properties", {})
        req = s.get("required", list(props.keys()))
        data: Dict[str, Any] = {}
        for name in req:
            data[name] = _example_from_schema(props.get(name, {"type": "string"}), spec)
        return data

    # default fallback
    return "x"


def _first_path_with_method(paths: Dict[str, Any], method: str) -> Optional[str]:
    for p, ops in paths.items():
        if method in ops:
            return p
    return None


def _extract_id(data: Any) -> Optional[int]:
    if isinstance(data, dict):
        for k in ("order_id", "id", "orderId"):
            v = data.get(k)
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.isdigit():
                return int(v)
        for v in data.values():  # last resort
            if isinstance(v, int):
                return v
    return None


# ---------------------------- the test ----------------------------------------
def test_openapi_driven_order_crud_happy_path():
    # Load openapi and locate orders paths
    spec = client.get("/openapi.json").json()
    orders_paths = _find_orders_paths(spec)
    assert orders_paths, "No paths containing 'orders' found in OpenAPI spec"

    # Find a POST path for creating
    post_path = _first_path_with_method(orders_paths, "post")
    assert post_path, f"No POST operation under: {list(orders_paths)}"

    post_op = orders_paths[post_path]["post"]
    # Build minimal valid payload from the requestBody schema
    req = (
        post_op.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
    )
    payload = _example_from_schema(req, spec) if req else {}

    # Create
    r_create = client.post(post_path, json=payload)
    assert r_create.status_code in (200, 201), f"Create failed: {r_create.text}"
    created = r_create.json()
    oid = _extract_id(created)
    assert oid is not None, f"Could not find id in create response: {created}"

    # Find a GET-by-id path (…/orders/{…})
    get_id_path = None
    for p, ops in orders_paths.items():
        if "{" in p and "get" in ops:
            get_id_path = p
            break
    assert get_id_path, f"No GET-by-id under: {list(orders_paths)}"

    r_get = client.get(get_id_path.replace("{", "").replace("}", "").replace("id", str(oid)).replace("order_id", str(oid)))
    # If the simple replacement above fails (e.g., parameter name is different),
    # try a generic replacement by substituting the first {...} with the id:
    if r_get.status_code >= 400:
        segs = list(get_id_path)
        # replace first {...} with /{oid}
        built = ""
        inside = False
        replaced = False
        for ch in segs:
            if ch == "{":
                inside = True
                if not replaced:
                    built += str(oid)
                    replaced = True
                continue
            if ch == "}":
                inside = False
                continue
            if not inside:
                built += ch
        r_get = client.get(built)

    assert r_get.status_code == 200, f"GET failed: {r_get.text}"
    body = r_get.json()
    assert isinstance(body, dict)

    # UPDATE if PUT/PATCH exists (we’ll just send the same payload)
    for m in ("put", "patch"):
        upd_path = None
        for p, ops in orders_paths.items():
            if "{" in p and m in ops:
                upd_path = p
                break
        if upd_path:
            upd_url = upd_path.replace("{", "").replace("}", "").replace("id", str(oid)).replace("order_id", str(oid))
            r_upd = client.request(m.upper(), upd_url, json=payload)
            assert r_upd.status_code in (200, 202, 204), f"{m.upper()} failed: {r_upd.text}"
            break

    # DELETE if available
    del_path = None
    for p, ops in orders_paths.items():
        if "{" in p and "delete" in ops:
            del_path = p
            break
    if del_path:
        del_url = del_path.replace("{", "").replace("}", "").replace("id", str(oid)).replace("order_id", str(oid))
        r_del = client.delete(del_url)
        assert r_del.status_code in (200, 202, 204), f"DELETE failed: {r_del.text}"
