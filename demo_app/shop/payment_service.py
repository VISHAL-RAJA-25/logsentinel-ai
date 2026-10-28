import logging
import os

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
    receipt_dir = "/var/shop/receipts"
    os.makedirs(receipt_dir, exist_ok=True)
    with open(os.path.join(receipt_dir, f"{payment.id}.txt"), "w") as f:
        f.write(f"Payment {payment.id}: {amount}")
    return payment
