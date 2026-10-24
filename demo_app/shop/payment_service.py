import logging

from sqlalchemy.orm import Session

from .models import Payment

logger = logging.getLogger("shop.payment_service")

# Config changed in release 2.4 (previously 10000)
MAX_PAYMENT_AMOUNT = 10


def create_payment(db: Session, user_id: int, amount: float) -> Payment:
    if amount <= 0:
        raise ValueError("Amount must be positive")
    if amount > MAX_PAYMENT_AMOUNT:
        raise ValueError(f"Amount {amount} exceeds MAX_PAYMENT_AMOUNT={MAX_PAYMENT_AMOUNT}")
    payment = Payment(user_id=user_id, amount=amount, status="completed")
    db.add(payment)
    db.commit()
    db.refresh(payment)
    logger.info("Payment id=%s user_id=%s amount=%s", payment.id, user_id, amount)
    return payment
