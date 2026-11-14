"""Schema for the IBM HI-Small ingestion tables (F01/F02).

Design notes are in docs/features/F02-ingestion.md.
"""

from datetime import date, datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
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
from sqlalchemy.dialects.postgresql import ARRAY
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


# ---- F03: synthetic KYC, sanctions and adverse media ----


class KycProfile(Base):
    __tablename__ = "kyc_profiles"
    __table_args__ = (
        CheckConstraint(
            "risk_rating IN ('low','medium','high')", name="ck_kyc_risk_rating"
        ),
    )

    entity_id: Mapped[str] = mapped_column(
        String(16), ForeignKey("entities.entity_id"), primary_key=True
    )
    full_name: Mapped[str] = mapped_column(Text)
    occupation: Mapped[str] = mapped_column(Text)
    country: Mapped[str] = mapped_column(String(2))
    expected_monthly_volume: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    risk_rating: Mapped[str] = mapped_column(String(8))


class SanctionsEntity(Base):
    """OpenSanctions rows that could plausibly be customers."""

    __tablename__ = "sanctions_entities"
    __table_args__ = (
        Index(
            "ix_sanctions_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )

    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text))
    schema_type: Mapped[str] = mapped_column(Text)
    countries: Mapped[list[str]] = mapped_column(ARRAY(Text))
    dataset: Mapped[list[str]] = mapped_column(ARRAY(Text))
    first_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PlantedMatch(Base):
    """Answer key for F15: which customers were deliberately given a sanctioned name."""

    __tablename__ = "planted_matches"
    __table_args__ = (
        CheckConstraint(
            "variant_kind IN ('exact','reordered','typo')", name="ck_planted_variant"
        ),
    )

    entity_id: Mapped[str] = mapped_column(
        String(16), ForeignKey("entities.entity_id"), primary_key=True
    )
    sanctions_source_id: Mapped[str] = mapped_column(
        Text, ForeignKey("sanctions_entities.source_id")
    )
    variant_kind: Mapped[str] = mapped_column(String(16))


class AdverseMedia(Base):
    __tablename__ = "adverse_media"
    __table_args__ = (
        Index("ix_adverse_media_entity_id", "entity_id"),
        Index(
            "ix_adverse_media_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    media_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_id: Mapped[str] = mapped_column(String(16), ForeignKey("entities.entity_id"))
    headline: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    published_at: Mapped[date] = mapped_column(Date)
    is_decoy: Mapped[bool] = mapped_column(Boolean)
    theme: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536))


# ---------------------------------------------------------------- F04: rules engine


class Alert(Base):
    """One alert = one account flagged by one rule in one tumbling time window."""

    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint(
            "rule_id",
            "account_id",
            "window_start",
            name="uq_alerts_rule_account_window",
        ),
        Index("ix_alerts_rule_id", "rule_id"),
        Index("ix_alerts_account_id", "account_id"),
    )

    alert_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rule_id: Mapped[str] = mapped_column(Text)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.account_id"))
    window_start: Mapped[datetime] = mapped_column(DateTime)
    window_end: Mapped[datetime] = mapped_column(DateTime)
    trigger_txn_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))
    score: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("now()"))
    # Ground-truth label, derived from transactions.is_laundering / laundering_attempts.
    is_true_positive: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false")
    )
    typology: Mapped[str | None] = mapped_column(Text, nullable=True)
