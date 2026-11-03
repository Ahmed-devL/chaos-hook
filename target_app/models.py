"""Target application schema from architecture Section 1."""

from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(String(64), primary_key=True)
    email = Column(String(120), nullable=False)
    stripe_customer_id = Column(String(64), index=True)
    plan_tier = Column(String(32), default="free")
    credit_balance = Column(Integer, default=0)
    welcome_email_sent = Column(Integer, default=0)
    last_event_ts = Column(Integer, default=0)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(String(64), primary_key=True)
    user_id = Column(String(64), index=True, nullable=False)
    stripe_customer_id = Column(String(64), index=True, nullable=False)
    status = Column(String(32), nullable=False)
    current_period_end = Column(Integer, default=0)
    last_event_ts = Column(Integer, default=0)


class CreditLedger(Base):
    __tablename__ = "credit_ledger"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(64), nullable=False, index=True)
    invoice_id = Column(String(64), nullable=False)
    delta = Column(Integer, nullable=False)
    reason = Column(String(64), nullable=False)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    event_id = Column(String(64), primary_key=True)
    event_type = Column(String(64), nullable=False)
    created_ts = Column(Integer, nullable=False)
    status = Column(String(32), default="processed")


class OutboxEmail(Base):
    __tablename__ = "outbox_emails"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(64), nullable=False)
    template = Column(String(64), nullable=False)
    dedupe_key = Column(String(128), nullable=True, unique=False)
