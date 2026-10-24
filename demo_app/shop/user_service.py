import hashlib
import logging

from sqlalchemy.orm import Session

from .models import User

logger = logging.getLogger("shop.user_service")


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def get_user_by_email(db: Session, email: str):
    return db.query(User).filter(User.email == email).first()


def create_user(db: Session, email: str, name: str, password: str) -> User:
    user = User(email=email, name=name, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("Created user id=%s email=%s", user.id, user.email)
    return user


def authenticate(db: Session, email: str, password: str):
    user = get_user_by_email(db, email)
    if user is None:
        logger.warning("Login attempt for unknown email=%s", email)
        return None
    if user.password_hash != hash_password(password):
        logger.warning("Failed login for email=%s", email)
        return None
    logger.info("Login success for user id=%s", user.id)
    return user
