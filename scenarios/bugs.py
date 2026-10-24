"""Each bug = a code change (on its own git branch) + the requests that
trigger it + the correct answer (ground truth) used later for evaluation."""

USER = "demo_app/shop/user_service.py"
PAY = "demo_app/shop/payment_service.py"
DB = "demo_app/shop/database.py"

SIGNUP = {"method": "POST", "path": "/signup",
          "json": {"email": "a@test.com", "name": "Alice", "password": "secret123"}}
PAY_OK = {"method": "POST", "path": "/payments",
          "json": {"user_id": 1, "amount": 49.99}}

BUGS = {
    "002": {
        "branch": "bug/002-wrong-column-query",
        "message": "Rename email lookup column to username",
        "edits": [(USER, "User.email == email", "User.username == email")],
        "title": "Signup crashes: query uses a column that does not exist",
        "requests": [SIGNUP],
        "ground_truth": {
            "category": "Database",
            "error_type": "AttributeError",
            "file": USER,
            "function": "get_user_by_email",
            "root_cause": "The query filters on User.username, but the User model has no such column; the column is called email",
            "correct_fix_keywords": ["email", "username", "column"],
        },
    },
    "003": {
        "branch": "bug/003-missing-env-var",
        "message": "Read payment API key from environment",
        "edits": [
            (PAY, "import logging\n\nfrom sqlalchemy.orm import Session",
                  "import logging\nimport os\n\nfrom sqlalchemy.orm import Session"),
            (PAY, "    if amount <= 0:",
                  '    api_key = os.environ["PAYMENT_API_KEY"]\n    if amount <= 0:'),
        ],
        "title": "Payment crashes: required environment variable is not set",
        "requests": [SIGNUP, PAY_OK],
        "ground_truth": {
            "category": "Configuration",
            "error_type": "KeyError",
            "file": PAY,
            "function": "create_payment",
            "root_cause": "create_payment reads the PAYMENT_API_KEY environment variable, which is not set in this environment",
            "correct_fix_keywords": ["PAYMENT_API_KEY", "environment variable", "set"],
        },
    },
    "004": {
        "branch": "bug/004-foreign-key-violation",
        "message": "Enable foreign key enforcement",
        "edits": [(DB, "Base = declarative_base()", '''from sqlalchemy import event


@event.listens_for(engine, "connect")
def _enable_foreign_keys(dbapi_connection, connection_record):
    dbapi_connection.execute("PRAGMA foreign_keys=ON")


Base = declarative_base()''')],
        "title": "Payment for a non-existent user fails with a database error",
        "requests": [SIGNUP, {"method": "POST", "path": "/payments",
                              "json": {"user_id": 999, "amount": 25.0}}],
        "ground_truth": {
            "category": "Database",
            "error_type": "IntegrityError",
            "file": PAY,
            "function": "create_payment",
            "introduced_in": DB,
            "root_cause": "Foreign key enforcement was enabled in database.py, but create_payment never checks that the user exists, so a payment for a missing user violates the FOREIGN KEY constraint",
            "correct_fix_keywords": ["user exists", "foreign key", "validate user"],
        },
    },
    "005": {
        "branch": "bug/005-bad-config-limit",
        "message": "Add payment amount limit (release 2.4)",
        "edits": [
            (PAY, 'logger = logging.getLogger("shop.payment_service")',
                  'logger = logging.getLogger("shop.payment_service")\n\n'
                  '# Config changed in release 2.4 (previously 10000)\n'
                  'MAX_PAYMENT_AMOUNT = 10'),
            (PAY, '    if amount <= 0:\n        raise ValueError("Amount must be positive")',
                  '    if amount <= 0:\n        raise ValueError("Amount must be positive")\n'
                  '    if amount > MAX_PAYMENT_AMOUNT:\n'
                  '        raise ValueError(f"Amount {amount} exceeds MAX_PAYMENT_AMOUNT={MAX_PAYMENT_AMOUNT}")'),
        ],
        "title": "Normal payments rejected after a bad configuration value",
        "requests": [SIGNUP, PAY_OK],
        "ground_truth": {
            "category": "Configuration",
            "error_type": "ValueError",
            "file": PAY,
            "function": "create_payment",
            "root_cause": "MAX_PAYMENT_AMOUNT was set to 10 in the latest release (previously 10000), so normal payments are rejected",
            "correct_fix_keywords": ["MAX_PAYMENT_AMOUNT", "limit", "config"],
        },
    },
}
