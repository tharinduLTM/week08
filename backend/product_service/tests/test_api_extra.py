# backend/product_service/tests/test_api_extra.py

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

# Uses fixtures defined in test_main.py: client, db_session_for_test, mock_azure_blob_storage (autouse)

def _create_product(client: TestClient, name="HD Widget", price=9.99, stock=10):
    resp = client.post(
        "/products/",
        json={
            "name": name,
            "description": "extra tests",
            "price": float(price),
            "stock_quantity": stock,
            "image_url": "http://example.com/seed.jpg",
        },
    )
    assert resp.status_code in (200, 201)
    return resp.json()["product_id"]

def test_get_product_not_found(client: TestClient):
    r = client.get("/products/99999999")
    assert r.status_code == 404
    assert "Product not found" in r.text

def test_update_product_success(client: TestClient, db_session_for_test: Session):
    pid = _create_product(client, name="Old", price=5.0, stock=3)
    r = client.put(
        f"/products/{pid}",
        json={
            "name": "New Name",
            "description": "updated desc",
            "price": 7.5,
            "stock_quantity": 8,
            "image_url": "http://example.com/new.png",
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "New Name"
    assert data["description"] == "updated desc"
    assert float(data["price"]) == 7.5
    assert data["stock_quantity"] == 8
    assert data["product_id"] == pid

def test_update_product_not_found(client: TestClient):
    r = client.put(
        "/products/42424242",
        json={"name": "x", "description": "y", "price": 1.0, "stock_quantity": 1},
    )
    assert r.status_code == 404

def test_upload_image_success(client: TestClient):
    # BlobServiceClient + generate_blob_sas are mocked by autouse fixture in test_main.py
    pid = _create_product(client)
    files = {
        "file": ("img.jpg", b"\xff\xd8\xff\xdb\x00C", "image/jpeg")  # tiny jpeg header
    }
    r = client.post(f"/products/{pid}/upload-image", files=files)
    assert r.status_code == 200
    url = r.json().get("image_url", "")
    # From the mock in test_main.py
    assert "mock_blob.jpg" in url or "sig=mock_sas_token" in url

def test_upload_image_invalid_type(client: TestClient):
    pid = _create_product(client)
    files = {"file": ("notes.txt", b"hello", "text/plain")}
    r = client.post(f"/products/{pid}/upload-image", files=files)
    assert r.status_code == 400

def test_deduct_stock_success(client: TestClient):
    pid = _create_product(client, stock=10)
    r = client.patch(
        f"/products/{pid}/deduct-stock", json={"quantity_to_deduct": 3}
    )
    assert r.status_code == 200
    assert r.json()["stock_quantity"] == 7

def test_deduct_stock_insufficient(client: TestClient):
    pid = _create_product(client, stock=2)
    r = client.patch(
        f"/products/{pid}/deduct-stock", json={"quantity_to_deduct": 5}
    )
    assert r.status_code == 400
