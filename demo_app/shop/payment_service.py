import logging

import httpx
from sqlalchemy.orm import Session

from .models import Payment

logger = logging.getLogger("shop.payment_service")

GATEWAY_URL = "http://127.0.0.1:9"  # payment gateway


def create_payment(db: Session, user_id: int, amount: float) -> Payment:
    if amount <= 0:
        raise ValueError("Amount must be positive")
    httpx.post(f"{GATEWAY_URL}/charge", json={"user_id": user_id, "amount": amount}, timeout=3)
    payment = Payment(user_id=user_id, amount=amount, status="completed")
    db.add(payment)
    db.commit()
    db.refresh(payment)
    logger.info("Payment id=%s user_id=%s amount=%s", payment.id, user_id, amount)
    return payment
