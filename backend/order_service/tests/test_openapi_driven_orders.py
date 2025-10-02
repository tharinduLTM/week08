# backend/order_service/tests/test_openapi_driven_orders.py
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app, get_db  # type: ignore
from app.db import Base as DbBase
from app import models  # noqa: F401  # ensure models register with Base


# -------------------------- shared in-memory DB -------------------------------
engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,  # critical: one shared connection for :memory:
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


# ------------------------------- helpers --------------------------------------
def _find_orders_paths(spec: Dict[str, Any]) -> Dict[str, Any]:
    paths = spec.get("paths", {})
    return {k: v for k, v in paths.items() if "orders" in k}


def _resolve(ref: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    assert ref.startswith("#/"), f"Unsupported $ref: {ref}"
    node: Any = spec
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _example_from_schema(s: Dict[str, Any], spec: Dict[str, Any]) -> Any:
    # Resolve $ref
    if "$ref" in s:
        return _example_from_schema(_resolve(s["$ref"], spec), spec)
    # anyOf/oneOf/allOf: pick the first
    for key in ("anyOf", "oneOf", "allOf"):
        if key in s and isinstance(s[key], list) and s[key]:
            return _example_from_schema(s[key][0], spec)

    t = s.get("type")
    fmt = s.get("format")

    if "enum" in s and s["enum"]:
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
        return [_example_from_schema(s.get("items", {"type": "string"}), spec)]
    if t == "object" or ("properties" in s):
        props = s.get("properties", {})
        req = s.get("required", list(props.keys()))
        data: Dict[str, Any] = {}
        for name in req:
            data[name] = _example_from_schema(props.get(name, {"type": "string"}), spec)
        return data
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


# --------------------------- the test (with httpx mocks) ----------------------
def test_openapi_driven_order_crud_happy_path(monkeypatch):
    """
    Drive create -> get -> (put/patch) -> delete for orders using OpenAPI to build a valid payload,
    while mocking Product-service HTTP calls so CI doesn't need a live Product service.
    """
    # --- Mock outbound calls to Product service (the app uses httpx) ---
    import httpx

    def _mock_json(url: str) -> dict:
        # A minimal product object many templates expect
        return {
            "id": 1,
            "name": "Mock Product",
            "price": 10.0,
            "stock_quantity": 99,
            "description": "test",
            "image_url": "http://example.com/x.png",
        }

    def _mk_response(status: int, url: str, json_obj: Optional[dict] = None) -> httpx.Response:
        return httpx.Response(status, request=httpx.Request("GET", url), json=json_obj)

    # Save originals in case fall-through is needed
    _orig_httpx_get = httpx.get
    _orig_httpx_post = httpx.post
    _orig_client_get = httpx.Client.get
    _orig_client_post = httpx.Client.post

    def _is_product_url(url: str) -> bool:
        u = url.lower()
        return "product" in u or "/products" in u

    def _fake_httpx_get(url, *a, **kw):
        if isinstance(url, httpx.URL):
            url = str(url)
        if _is_product_url(url):
            return _mk_response(200, url, _mock_json(url))
        return _orig_httpx_get(url, *a, **kw)

    def _fake_httpx_post(url, *a, **kw):
        if isinstance(url, httpx.URL):
            url = str(url)
        if _is_product_url(url):
            # e.g., stock reservation/validation
            return _mk_response(200, url, {"ok": True})
        return _orig_httpx_post(url, *a, **kw)

    def _fake_client_get(self, url, *a, **kw):
        if isinstance(url, httpx.URL):
            url = str(url)
        if _is_product_url(url):
            return _mk_response(200, url, _mock_json(url))
        return _orig_client_get(self, url, *a, **kw)

    def _fake_client_post(self, url, *a, **kw):
        if isinstance(url, httpx.URL):
            url = str(url)
        if _is_product_url(url):
            return _mk_response(200, url, {"ok": True})
        return _orig_client_post(self, url, *a, **kw)

    monkeypatch.setattr(httpx, "get", _fake_httpx_get)
    monkeypatch.setattr(httpx, "post", _fake_httpx_post)
    monkeypatch.setattr(httpx.Client, "get", _fake_client_get)
    monkeypatch.setattr(httpx.Client, "post", _fake_client_post)

    # --- Drive the API via OpenAPI ---
    spec = client.get("/openapi.json").json()
    orders_paths = _find_orders_paths(spec)
    assert orders_paths, "No paths containing 'orders' found in OpenAPI spec"

    # POST (create)
    post_path = _first_path_with_method(orders_paths, "post")
    assert post_path, f"No POST operation under: {list(orders_paths)}"
    post_op = orders_paths[post_path]["post"]
    req_schema = (
        post_op.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
    )
    payload = _example_from_schema(req_schema, spec) if req_schema else {}

    r_create = client.post(post_path, json=payload)
    assert r_create.status_code in (200, 201), f"Create failed: {r_create.text}"
    created = r_create.json()
    oid = _extract_id(created)
    assert oid is not None, f"Could not find id in create response: {created}"

    # GET by id (replace the first {param} with oid)
    get_id_path = None
    for p, ops in orders_paths.items():
        if "{" in p and "get" in ops:
            get_id_path = p
            break
    assert get_id_path, f"No GET-by-id under: {list(orders_paths)}"
    built_get = re.sub(r"\{[^}]+\}", str(oid), get_id_path, count=1)
    r_get = client.get(built_get)
    assert r_get.status_code == 200, f"GET failed: {r_get.text}"

    # PUT/PATCH if available (send back same payload; many APIs accept it)
    for method in ("put", "patch"):
        upd_path = None
        for p, ops in orders_paths.items():
            if "{" in p and method in ops:
                upd_path = p
                break
        if upd_path:
            upd_url = re.sub(r"\{[^}]+\}", str(oid), upd_path, count=1)
            r_upd = client.request(method.upper(), upd_url, json=payload)
            assert r_upd.status_code in (200, 202, 204), f"{method.upper()} failed: {r_upd.text}"
            break

    # DELETE if available
    del_path = None
    for p, ops in orders_paths.items():
        if "{" in p and "delete" in ops:
            del_path = p
            break
    if del_path:
        del_url = re.sub(r"\{[^}]+\}", str(oid), del_path, count=1)
        r_del = client.delete(del_url)
        assert r_del.status_code in (200, 202, 204), f"DELETE failed: {r_del.text}"
