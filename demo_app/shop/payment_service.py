import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from .models import Payment

logger = logging.getLogger("shop.payment_service")


def create_payment(db: Session, user_id: int, amount: float) -> Payment:
    if amount <= 0:
        raise ValueError("Amount must be positive")
    payment = Payment(user_id=user_id, amount=amount, status="completed")
    db.add(payment)
    db.commit()
    db.refresh(payment)
    logger.info("Payment id=%s user_id=%s amount=%s", payment.id, user_id, amount)
    count = db.execute(
        text("SELECT COUNT(*) FROM payment WHERE user_id = :uid"), {"uid": user_id}
    ).scalar()
    logger.info("User %s now has %s payments", user_id, count)
    return payment
