import logging

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from . import payment_service, user_service
from .database import Base, engine, get_db
from .logging_config import setup_logging

setup_logging()
logger = logging.getLogger("shop.api")

Base.metadata.create_all(bind=engine)
app = FastAPI(title="Demo Shop")


class SignupIn(BaseModel):
    email: str
    name: str
    password: str


class LoginIn(BaseModel):
    email: str
    password: str


class PaymentIn(BaseModel):
    user_id: int
    amount: float


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/signup", status_code=201)
def signup(body: SignupIn, db: Session = Depends(get_db)):
    if user_service.get_user_by_email(db, body.email):
        raise HTTPException(status_code=409, detail="Email already registered")
    user = user_service.create_user(db, body.email, body.name, body.password)
    return {"id": user.id, "email": user.email}


@app.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = user_service.authenticate(db, body.email, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"message": "Login successful", "user_id": user.id}


@app.post("/payments", status_code=201)
def pay(body: PaymentIn, db: Session = Depends(get_db)):
    payment = payment_service.create_payment(db, body.user_id, body.amount)
    return {"id": payment.id, "status": payment.status}
