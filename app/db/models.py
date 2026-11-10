"""Schema for the IBM HI-Small ingestion tables (F01/F02).

Design notes are in docs/features/F02-ingestion.md.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Bank(Base):
    __tablename__ = "banks"

    bank_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    # Bank names are NOT unique in the source data, so bank_id is the key.
    bank_name: Mapped[str] = mapped_column(Text)


class Entity(Base):
    """The customer. One entity owns one or more accounts."""

    __tablename__ = "entities"

    entity_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    entity_name: Mapped[str] = mapped_column(Text)
    # Parsed from the name prefix, e.g. "Corporation #33520" -> "Corporation".
    entity_type: Mapped[str] = mapped_column(String(32))


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        # Account numbers alone are not unique (8 collisions across banks).
        UniqueConstraint("bank_id", "account_number", name="uq_accounts_bank_account"),
        Index("ix_accounts_entity_id", "entity_id"),
    )

    account_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    bank_id: Mapped[int] = mapped_column(ForeignKey("banks.bank_id"))
    account_number: Mapped[str] = mapped_column(String(16))
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.entity_id"))


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_from_ts", "from_account_id", "ts"),
        Index("ix_transactions_to_ts", "to_account_id", "ts"),
        Index(
            "ix_transactions_laundering",
            "txn_id",
            postgresql_where=text("is_laundering"),
        ),
    )

    # Source CSV has no ID: txn_id = data-row number in the file, so reloads
    # are reproducible.
    txn_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime
    )  # minute resolution, no timezone in source
    from_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.account_id"))
    to_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.account_id"))
    # NUMERIC, never float: source has values down to 0.000001.
    amount_received: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    receiving_currency: Mapped[str] = mapped_column(String(32))
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    payment_currency: Mapped[str] = mapped_column(String(32))
    payment_format: Mapped[str] = mapped_column(String(32))
    is_laundering: Mapped[bool] = mapped_column(Boolean)


class LaunderingAttempt(Base):
    __tablename__ = "laundering_attempts"

    attempt_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=False
    )
    # FAN-OUT, FAN-IN, CYCLE, GATHER-SCATTER, SCATTER-GATHER, BIPARTITE, STACK, RANDOM
    typology: Mapped[str] = mapped_column(String(32), index=True)
    # Original header text, e.g. "FAN-OUT:  Max 16-degree Fan-Out"
    detail_raw: Mapped[str] = mapped_column(Text)
    # The "N" in "Max N-degree" / "Max N hops"; NULL when the header has none.
    max_degree_or_hops: Mapped[int | None] = mapped_column(Integer, nullable=True)


class LaunderingAttemptTransaction(Base):
    __tablename__ = "laundering_attempt_transactions"
    __table_args__ = (Index("ix_lat_txn_id", "txn_id"),)

    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("laundering_attempts.attempt_id"), primary_key=True
    )
    txn_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.txn_id"), primary_key=True
    )
