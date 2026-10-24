def signup(client, email="a@test.com"):
    return client.post(
        "/signup", json={"email": email, "name": "Alice", "password": "secret123"}
    )


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_signup_success(client):
    r = signup(client)
    assert r.status_code == 201
    assert r.json()["email"] == "a@test.com"


def test_signup_duplicate(client):
    signup(client)
    assert signup(client).status_code == 409


def test_login_success(client):
    signup(client)
    r = client.post("/login", json={"email": "a@test.com", "password": "secret123"})
    assert r.status_code == 200


def test_login_wrong_password(client):
    signup(client)
    r = client.post("/login", json={"email": "a@test.com", "password": "wrong"})
    assert r.status_code == 401


def test_payment_success(client):
    user_id = signup(client).json()["id"]
    r = client.post("/payments", json={"user_id": user_id, "amount": 49.99})
    assert r.status_code == 201
    assert r.json()["status"] == "completed"
