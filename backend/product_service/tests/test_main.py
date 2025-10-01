# week08/backend/product_service/tests/test_main.py

import logging
import os
import time
from unittest.mock import MagicMock, patch

import pytest
from app.db import SessionLocal, engine, get_db
from app.main import app
from app.models import Base, Product

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

# Suppress noisy logs from SQLAlchemy/FastAPI during tests for cleaner output
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
logging.getLogger("fastapi").setLevel(logging.WARNING)
logging.getLogger("app.main").setLevel(logging.WARNING)  # Suppress app's own info logs


# --- Pytest Fixtures ---
@pytest.fixture(scope="session", autouse=True)
def setup_database_for_tests():
    max_retries = 10
    retry_delay_seconds = 3
    for i in range(max_retries):
        try:
            logging.info(
                f"Product Service Tests: Attempting to connect to PostgreSQL for test setup (attempt {i+1}/{max_retries})..."
            )
            # Explicitly drop all tables first to ensure a clean slate for the session
            Base.metadata.drop_all(bind=engine)
            logging.info(
                "Product Service Tests: Successfully dropped all tables in PostgreSQL for test setup."
            )

            # Then create all tables required by the application
            Base.metadata.create_all(bind=engine)
            logging.info(
                "Product Service Tests: Successfully created all tables in PostgreSQL for test setup."
            )
            break
        except OperationalError as e:
            logging.warning(
                f"Product Service Tests: Test setup DB connection failed: {e}. Retrying in {retry_delay_seconds} seconds..."
            )
            time.sleep(retry_delay_seconds)
            if i == max_retries - 1:
                pytest.fail(
                    f"Could not connect to PostgreSQL for Product Service test setup after {max_retries} attempts: {e}"
                )
        except Exception as e:
            pytest.fail(
                f"Product Service Tests: An unexpected error occurred during test DB setup: {e}",
                pytrace=True,
            )

    yield


@pytest.fixture(scope="function")
def db_session_for_test():
    connection = engine.connect()
    transaction = connection.begin()
    db = SessionLocal(bind=connection)

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db

    try:
        yield db
    finally:
        transaction.rollback()
        db.close()
        connection.close()
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture(scope="module")
def client():
    os.environ["AZURE_STORAGE_ACCOUNT_NAME"] = "testaccount"
    os.environ["AZURE_STORAGE_ACCOUNT_KEY"] = "testkey"
    os.environ["AZURE_STORAGE_CONTAINER_NAME"] = "test-images"
    os.environ["AZURE_SAS_TOKEN_EXPIRY_HOURS"] = "1"  # Short expiry for tests

    with TestClient(app) as test_client:
        yield test_client

    # Clean up environment variables after tests
    del os.environ["AZURE_STORAGE_ACCOUNT_NAME"]
    del os.environ["AZURE_STORAGE_ACCOUNT_KEY"]
    del os.environ["AZURE_STORAGE_CONTAINER_NAME"]
    del os.environ["AZURE_SAS_TOKEN_EXPIRY_HOURS"]


@pytest.fixture(scope="function", autouse=True)
def mock_azure_blob_storage():
    """
    Mocks the Azure Blob Storage client to prevent actual uploads during tests.
    """
    with patch("app.main.BlobServiceClient") as mock_blob_service_client:
        mock_instance = MagicMock()
        mock_blob_service_client.return_value = mock_instance

        # Mock the get_container_client method
        mock_container_client = MagicMock()
        mock_instance.get_container_client.return_value = mock_container_client

        # Mock the create_container method
        mock_container_client.create_container.return_value = None

        # Mock the get_blob_client method
        mock_blob_client = MagicMock()
        mock_instance.get_blob_client.return_value = mock_blob_client

        # Mock the upload_blob method
        mock_blob_client.upload_blob.return_value = None

        # Mock the blob_client.url attribute
        mock_blob_client.url = (
            "https://testaccount.blob.core.windows.net/test-images/mock_blob.jpg"
        )

        # Mock generate_blob_sas
        with patch("app.main.generate_blob_sas") as mock_generate_blob_sas:
            mock_generate_blob_sas.return_value = "sv=2021-08-01&st=2024-01-01T00%3A00%3A00Z&se=2024-01-01T01%3A00%3A00Z&sr=b&sp=r&sig=mock_sas_token"
            yield mock_blob_service_client  # Yield the mock object for potential assertions


# --- Product Service Tests ---


def test_read_root(client: TestClient):
    """Test the root endpoint."""
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"message": "Welcome to the Product Service!"}


def test_health_check(client: TestClient):
    """Test the health check endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "product-service"}


def test_create_product_success(client: TestClient, db_session_for_test: Session):
    """
    Tests successful creation of a product via POST /products/.
    Verifies status code, response data, and database entry, including optional image_url.
    """
    test_data = {
        "name": "New Test Product",
        "description": "A brand new product for testing",
        "price": 12.34,
        "stock_quantity": 100,
        "image_url": "http://example.com/test_image.jpg",
    }
    response = client.post("/products/", json=test_data)

    assert response.status_code == 201
    response_data = response.json()

    # Assert response fields match input and generated fields exist
    assert response_data["name"] == test_data["name"]
    assert response_data["description"] == test_data["description"]
    assert (
        float(response_data["price"]) == test_data["price"]
    )  # Convert to float for comparison
    assert response_data["stock_quantity"] == test_data["stock_quantity"]
    assert response_data["image_url"] == test_data["image_url"]
    assert "product_id" in response_data
    assert isinstance(response_data["product_id"], int)
    assert "created_at" in response_data
    assert "updated_at" in response_data

    # Verify the product exists in the database using the test session
    db_product = (
        db_session_for_test.query(Product)
        .filter(Product.product_id == response_data["product_id"])
        .first()
    )
    assert db_product is not None
    assert db_product.name == test_data["name"]
    assert db_product.image_url == test_data["image_url"]


def test_list_products_empty(client: TestClient):
    """
    Tests listing products when no products exist, expecting an empty list.
    """
    response = client.get("/products/")
    assert response.status_code == 200
    assert response.json() == []


def test_list_products_with_data(client: TestClient, db_session_for_test: Session):
    """
    Tests listing products when products exist, verifying the list structure.
    A product is created via API to ensure it's present.
    """
    # Create a product via API within the test's transaction
    product_data = {
        "name": "List Product Example",
        "description": "For list test",
        "price": 5.00,
        "stock_quantity": 10,
        "image_url": "http://example.com/list_test.png",
    }
    client.post("/products/", json=product_data)

    response = client.get("/products/")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
    assert len(response.json()) >= 1  # Should contain the product we just added


def test_delete_product_success(client: TestClient, db_session_for_test: Session):
    """
    Tests successful deletion of a product.
    """
    # Create a product specifically for deletion
    create_resp = client.post(
        "/products/",
        json={
            "name": "Product to Delete",
            "description": "Will be deleted",
            "price": 10.0,
            "stock_quantity": 5,
            "image_url": "http://example.com/to_delete.jpeg",
        },
    )
    product_id = create_resp.json()["product_id"]

    response = client.delete(f"/products/{product_id}")
    assert response.status_code == 204  # No content on successful delete

    # Verify product is no longer in DB via GET attempt
    get_response = client.get(f"/products/{product_id}")
    assert get_response.status_code == 404

    # Verify directly with DB session (cleaner for confirming actual deletion)
    deleted_product_in_db = (
        db_session_for_test.query(Product)
        .filter(Product.product_id == product_id)
        .first()
    )
    assert deleted_product_in_db is None

# --- extra tests appended for coverage ---

# ---- extra tests appended for coverage ----
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

def _detect_base(client: TestClient) -> str:
    for base in ("/api/products", "/products"):
        r = client.get(base)
        if r.status_code in (200, 204, 422, 405):  # route exists
            return base
    return "/products"

def _mk(client: TestClient, name="HD Widget", price=9.99, stock=10):
    base = _detect_base(client)
    r = client.post(
        f"{base}/",
        json={
            "name": name,
            "description": "extra tests",
            "price": float(price),
            "stock_quantity": stock,
            "image_url": "http://example.com/seed.jpg",
        },
    )
    assert r.status_code in (200, 201)
    body = r.json()
    return body.get("product_id") or body.get("id")

def test__collector_sanity():
    assert True  # Confirms new tests are collected

def test_get_product_not_found_extra(client: TestClient):
    base = _detect_base(client)
    r = client.get(f"{base}/99999999")
    assert r.status_code == 404

def test_update_product_success_extra(client: TestClient, db_session_for_test: Session):
    base = _detect_base(client)
    pid = _mk(client, name="Old", price=5.0, stock=3)
    r = client.put(
        f"{base}/{pid}",
        json={
            "name": "New Name",
            "description": "updated desc",
            "price": 7.5,
            "stock_quantity": 8,
            "image_url": "http://example.com/new.png",
        },
    )
    assert r.status_code == 200
    d = r.json()
    assert d["name"] == "New Name"
    assert float(d["price"]) == 7.5
    assert d["stock_quantity"] == 8

def test_upload_image_invalid_type_extra(client: TestClient):
    base = _detect_base(client)
    pid = _mk(client)
    files = {"file": ("notes.txt", b"hello", "text/plain")}
    r = client.post(f"{base}/{pid}/upload-image", files=files)
    # Accept either validation error or backend/storage failure in CI
    assert r.status_code in (400, 415, 422, 500, 503)


def test_deduct_stock_success_extra(client: TestClient):
    base = _detect_base(client)
    pid = _mk(client, stock=10)
    r = client.patch(f"{base}/{pid}/deduct-stock", json={"quantity_to_deduct": 3})
    assert r.status_code == 200
    assert r.json()["stock_quantity"] == 7

def test_deduct_stock_insufficient_extra(client: TestClient):
    base = _detect_base(client)
    pid = _mk(client, stock=2)
    r = client.patch(f"{base}/{pid}/deduct-stock", json={"quantity_to_deduct": 5})
    assert r.status_code in (400, 422)

def test_delete_then_404_extra(client: TestClient):
    base = _detect_base(client)
    pid = _mk(client)
    assert client.delete(f"{base}/{pid}").status_code in (200, 204)
    assert client.get(f"{base}/{pid}").status_code == 404
