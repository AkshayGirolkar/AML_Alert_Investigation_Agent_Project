"""F14: console.v_alert_transactions (the transactions behind each alert)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-26 15:00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# One row per (alert, trigger transaction). The ground-truth flag (is_laundering) is
# deliberately not selected.
VIEW = """
CREATE VIEW console.v_alert_transactions AS
SELECT a.alert_id,
       t.txn_id,
       t.ts,
       t.from_account_id,
       fa.account_number AS from_account_number,
       fb.bank_name AS from_bank,
       t.to_account_id,
       ta.account_number AS to_account_number,
       tb.bank_name AS to_bank,
       t.amount_paid,
       t.payment_currency,
       t.amount_received,
       t.receiving_currency,
       t.payment_format
FROM alerts a
CROSS JOIN LATERAL unnest(a.trigger_txn_ids) AS u(txn_id)
JOIN transactions t ON t.txn_id = u.txn_id
JOIN accounts fa ON fa.account_id = t.from_account_id
JOIN banks fb ON fb.bank_id = fa.bank_id
JOIN accounts ta ON ta.account_id = t.to_account_id
JOIN banks tb ON tb.bank_id = ta.bank_id
"""


def upgrade() -> None:
    op.execute(VIEW)
    op.execute("GRANT SELECT ON console.v_alert_transactions TO console_app")


def downgrade() -> None:
    op.execute("DROP VIEW console.v_alert_transactions")
