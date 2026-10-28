"""Bugs 006-015."""

USER = "demo_app/shop/user_service.py"
PAY = "demo_app/shop/payment_service.py"
DB = "demo_app/shop/database.py"
MAIN = "demo_app/shop/main.py"

SIGNUP = {"method": "POST", "path": "/signup",
          "json": {"email": "a@test.com", "name": "Alice", "password": "secret123"}}
LOGIN_OK = {"method": "POST", "path": "/login",
            "json": {"email": "a@test.com", "password": "secret123"}}
PAY_OK = {"method": "POST", "path": "/payments",
          "json": {"user_id": 1, "amount": 49.99}}

IMPORT_OS = ("import logging\n\nfrom sqlalchemy.orm import Session",
             "import logging\nimport os\n\nfrom sqlalchemy.orm import Session")
LOGGER_LINE = 'logger = logging.getLogger("shop.payment_service")'
PAYMENT_LINE = '    payment = Payment(user_id=user_id, amount=amount, status="completed")'
PAY_LOG = '    logger.info("Payment id=%s user_id=%s amount=%s", payment.id, user_id, amount)'
LOG_AND_RETURN = PAY_LOG + "\n    return payment"

BUGS2 = {
    "006": {
        "branch": "bug/006-db-pool-timeout",
        "message": "Add signup audit check and tune connection pool",
        "edits": [
            (DB, "engine = create_engine(DATABASE_URL, connect_args=connect_args)",
                 "engine = create_engine(\n    DATABASE_URL,\n    connect_args=connect_args,\n"
                 "    pool_size=1,\n    max_overflow=0,\n    pool_timeout=1,\n)"),
            (USER, "from .models import User\n",
                   "from sqlalchemy import text\n\nfrom .database import SessionLocal\nfrom .models import User\n"),
            (USER, "def create_user(db: Session, email: str, name: str, password: str) -> User:",
                   "def record_signup_audit(email: str) -> None:\n    audit_db = SessionLocal()\n    try:\n"
                   "        audit_db.execute(text(\"SELECT 1\"))\n    finally:\n        audit_db.close()\n\n\n"
                   "def create_user(db: Session, email: str, name: str, password: str) -> User:"),
            (USER, '    logger.info("Created user id=%s email=%s", user.id, user.email)\n    return user',
                   '    record_signup_audit(user.email)\n'
                   '    logger.info("Created user id=%s email=%s", user.id, user.email)\n    return user'),
        ],
        "title": "Signup times out waiting for a database connection",
        "requests": [SIGNUP],
        "ground_truth": {
            "category": "Timeout",
            "error_type": "TimeoutError",
            "file": USER,
            "function": "record_signup_audit",
            "introduced_in": DB,
            "root_cause": "The connection pool was shrunk to one connection with no overflow, and record_signup_audit opens a second session while the request session still holds that connection, so the checkout times out",
            "correct_fix_keywords": ["pool_size", "connection pool", "second session"],
        },
    },
    "007": {
        "branch": "bug/007-missing-dependency",
        "message": "Switch password hashing to argon2",
        "edits": [(USER, "    return hashlib.sha256(password.encode()).hexdigest()",
                         "    import argon2\n\n    return argon2.PasswordHasher().hash(password)")],
        "title": "Signup fails: required package is not installed",
        "requests": [SIGNUP],
        "ground_truth": {
            "category": "Deployment",
            "error_type": "ModuleNotFoundError",
            "file": USER,
            "function": "hash_password",
            "root_cause": "hash_password imports argon2, but the package was never added to requirements.txt or installed in this environment",
            "correct_fix_keywords": ["argon2", "requirements", "install"],
        },
    },
    "008": {
        "branch": "bug/008-fee-rate-string",
        "message": "Add payment processing fee",
        "edits": [
            (PAY, *IMPORT_OS),
            (PAY, LOGGER_LINE,
                  LOGGER_LINE + '\n\nFEE_RATE = os.getenv("FEE_RATE", "0.02")  # processing fee'),
            (PAY, PAYMENT_LINE,
                  '    fee = amount * FEE_RATE\n'
                  '    payment = Payment(user_id=user_id, amount=amount + fee, status="completed")'),
        ],
        "title": "Payment crashes: fee rate from config is a string",
        "requests": [SIGNUP, PAY_OK],
        "ground_truth": {
            "category": "Configuration",
            "error_type": "TypeError",
            "file": PAY,
            "function": "create_payment",
            "root_cause": "FEE_RATE is read with os.getenv, so it is a string, and create_payment multiplies the float amount by that string",
            "correct_fix_keywords": ["FEE_RATE", "float", "convert"],
        },
    },
    "009": {
        "branch": "bug/009-receipt-permission",
        "message": "Write payment receipts to disk",
        "edits": [
            (PAY, *IMPORT_OS),
            (PAY, LOG_AND_RETURN,
                  PAY_LOG + '\n    receipt_dir = "/var/shop/receipts"\n'
                  '    os.makedirs(receipt_dir, exist_ok=True)\n'
                  '    with open(os.path.join(receipt_dir, f"{payment.id}.txt"), "w") as f:\n'
                  '        f.write(f"Payment {payment.id}: {amount}")\n    return payment'),
        ],
        "title": "Payment fails after saving: cannot write receipt file",
        "requests": [SIGNUP, PAY_OK],
        "ground_truth": {
            "category": "Permission",
            "error_type": "PermissionError",
            "file": PAY,
            "function": "create_payment",
            "root_cause": "create_payment writes receipts to /var/shop/receipts, a directory the application user is not allowed to create",
            "correct_fix_keywords": ["permission", "receipt", "directory"],
        },
    },
    "010": {
        "branch": "bug/010-display-name-index",
        "message": "Store last name as display name on signup",
        "edits": [(USER, "    user = User(email=email, name=name, password_hash=hash_password(password))",
                         "    display_name = name.split()[1]  # show last name in the UI\n"
                         "    user = User(email=email, name=display_name, password_hash=hash_password(password))")],
        "title": "Signup crashes for users with a one-word name",
        "requests": [SIGNUP],
        "ground_truth": {
            "category": "Application Logic",
            "error_type": "IndexError",
            "file": USER,
            "function": "create_user",
            "root_cause": "create_user takes name.split()[1], which assumes the name has at least two words, but the name was a single word",
            "correct_fix_keywords": ["split", "index", "single word"],
        },
    },
    "011": {
        "branch": "bug/011-gateway-down",
        "message": "Charge payment gateway before saving payment",
        "edits": [
            (PAY, "import logging\n\nfrom sqlalchemy.orm import Session",
                  "import logging\n\nimport httpx\nfrom sqlalchemy.orm import Session"),
            (PAY, LOGGER_LINE,
                  LOGGER_LINE + '\n\nGATEWAY_URL = "http://127.0.0.1:9"  # payment gateway'),
            (PAY, PAYMENT_LINE,
                  '    httpx.post(f"{GATEWAY_URL}/charge", json={"user_id": user_id, "amount": amount}, timeout=3)\n'
                  + PAYMENT_LINE),
        ],
        "title": "Payments fail: payment gateway refuses connections",
        "requests": [SIGNUP, PAY_OK],
        "ground_truth": {
            "category": "Third-party API",
            "error_type": "ConnectError",
            "file": PAY,
            "function": "create_payment",
            "root_cause": "create_payment calls the payment gateway at GATEWAY_URL, but nothing is listening on that address, so the connection is refused",
            "correct_fix_keywords": ["gateway", "connection refused", "retry"],
        },
    },
    "012": {
        "branch": "bug/012-recursive-normalize",
        "message": "Normalize email before lookup",
        "edits": [
            (USER, "    return db.query(User).filter(User.email == email).first()",
                   "    return db.query(User).filter(User.email == normalize_email(email)).first()"),
            (USER, "def get_user_by_email(db: Session, email: str):",
                   "def normalize_email(email: str) -> str:\n    return normalize_email(email.strip().lower())\n\n\n"
                   "def get_user_by_email(db: Session, email: str):"),
        ],
        "title": "Signup crashes with maximum recursion depth exceeded",
        "requests": [SIGNUP],
        "ground_truth": {
            "category": "Application Logic",
            "error_type": "RecursionError",
            "file": USER,
            "function": "normalize_email",
            "root_cause": "normalize_email calls itself instead of returning the cleaned value, so it recurses until the recursion limit",
            "correct_fix_keywords": ["recursion", "normalize_email", "return"],
        },
    },
    "013": {
        "branch": "bug/013-decode-str",
        "message": "Prepare password comparison for bcrypt migration",
        "edits": [(USER, "    if user.password_hash != hash_password(password):",
                         "    if user.password_hash.decode() != hash_password(password):")],
        "title": "Every login crashes after password comparison change",
        "requests": [SIGNUP, LOGIN_OK],
        "ground_truth": {
            "category": "Authentication",
            "error_type": "AttributeError",
            "file": USER,
            "function": "authenticate",
            "root_cause": "authenticate calls .decode() on password_hash, which is already a str, so every login raises AttributeError",
            "correct_fix_keywords": ["decode", "str", "password_hash"],
        },
    },
    "014": {
        "branch": "bug/014-duplicate-signup",
        "message": "Remove redundant email lookup on signup",
        "edits": [(MAIN, '    if user_service.get_user_by_email(db, body.email):\n'
                         '        raise HTTPException(status_code=409, detail="Email already registered")\n', "")],
        "title": "Duplicate signup returns 500 with a unique constraint error",
        "requests": [SIGNUP, SIGNUP],
        "ground_truth": {
            "category": "Database",
            "error_type": "IntegrityError",
            "file": USER,
            "function": "create_user",
            "introduced_in": MAIN,
            "root_cause": "The email-exists check was removed from the signup route, so a duplicate email reaches the database and violates the UNIQUE constraint on users.email",
            "correct_fix_keywords": ["duplicate", "unique", "409"],
        },
    },
    "015": {
        "branch": "bug/015-wrong-table-sql",
        "message": "Log payment count after each payment",
        "edits": [
            (PAY, "from sqlalchemy.orm import Session",
                  "from sqlalchemy import text\nfrom sqlalchemy.orm import Session"),
            (PAY, LOG_AND_RETURN,
                  PAY_LOG + '\n    count = db.execute(\n'
                  '        text("SELECT COUNT(*) FROM payment WHERE user_id = :uid"), {"uid": user_id}\n'
                  '    ).scalar()\n'
                  '    logger.info("User %s now has %s payments", user_id, count)\n    return payment'),
        ],
        "title": "Payment saved but request fails: raw SQL uses wrong table",
        "requests": [SIGNUP, PAY_OK],
        "ground_truth": {
            "category": "Database",
            "error_type": "OperationalError",
            "file": PAY,
            "function": "create_payment",
            "root_cause": "The raw SQL query selects from a table named payment, but the table is called payments",
            "correct_fix_keywords": ["payments", "table name", "SQL"],
        },
    },
}
