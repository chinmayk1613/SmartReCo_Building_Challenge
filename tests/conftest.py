import os

os.environ["DATABASE_URL"] = "sqlite:///./.smartreco/pytest.db"
os.environ["QDRANT_PATH"] = "./.smartreco/pytest-qdrant"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["MESH_API_KEY"] = ""
os.environ["MESH_EMBEDDINGS_ENABLED"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGSMITH_API_KEY"] = ""

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Product, User
from app.schemas import ProductInput
from app.security import hash_password
from app.services.catalog import create_product


@pytest.fixture(scope="session", autouse=True)
def database_schema():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture(autouse=True)
def clean_database():
    db = SessionLocal()
    for table in reversed(Base.metadata.sorted_tables):
        db.execute(table.delete())
    db.commit()
    db.close()


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def user(db):
    value = User(email="learner@example.com", display_name="Learner", password_hash=hash_password("VeryStrong123!"))
    db.add(value)
    db.commit()
    return value


@pytest.fixture
def admin(db):
    value = User(email="admin@example.com", display_name="Admin", password_hash=hash_password("VeryStrong123!"), role="admin")
    db.add(value)
    db.commit()
    return value


@pytest.fixture
def products(db):
    values = []
    for index, category in enumerate(["Agentic AI", "Generative AI", "Python for AI", "MLOps", "Data Engineering", "Cloud & DevOps"]):
        data = ProductInput(
            title=f"{category} Mastery {index}", slug=f"course-{index}", description=f"A complete practical program for {category} with production projects and reliable evaluation.",
            category=category, level="Advanced" if index < 2 else "Intermediate", skills=[category, "testing"], outcomes=["Build a production project"],
            price=100 + index * 20, rating=4.5 + index * .05, popularity=900-index*50,
        )
        values.append(create_product(db, data))
    db.commit()
    return values


def login(client, email="learner@example.com", password="VeryStrong123!"):
    client.get("/login")
    return client.post("/login", data={"email": email, "password": password, "form_csrf": client.cookies.get("smartreco_auth_csrf")}, follow_redirects=False)
