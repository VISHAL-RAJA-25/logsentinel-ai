import os

os.environ["DATABASE_URL"] = "sqlite:///./test.db"
os.environ["APP_LOG_DIR"] = "test_logs"

import pytest
from fastapi.testclient import TestClient

from shop.database import Base, engine
from shop.main import app


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    return TestClient(app)
