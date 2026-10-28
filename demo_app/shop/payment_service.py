import logging
import os

from sqlalchemy.orm import Session

from .models import Payment

logger = logging.getLogger("shop.payment_service")

FEE_RATE = os.getenv("FEE_RATE", "0.02")  # processing fee


def create_payment(db: Session, user_id: int, amount: float) -> Payment:
    if amount <= 0:
        raise ValueError("Amount must be positive")
    fee = amount * FEE_RATE
    payment = Payment(user_id=user_id, amount=amount + fee, status="completed")
    db.add(payment)
    db.commit()
    db.refresh(payment)
    logger.info("Payment id=%s user_id=%s amount=%s", payment.id, user_id, amount)
    return payment
