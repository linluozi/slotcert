"""Deterministic instance factories for the ledger model."""

from .models import (
    Account,
    Customer,
    Invoice,
    LedgerEntry,
    Money,
    Payment,
    ReportRow,
    Transaction,
)

FACTORIES = {
    "Money": lambda: Money(1234, "USD"),
    "Account": lambda: Account("ACC-0001", "alice", 500_000),
    "Transaction": lambda: Transaction("TX-0001", "ACC-0001", 1299, "invoice #42"),
    "LedgerEntry": lambda: LedgerEntry(7, "TX-0001", 1299, 0),
    "Customer": lambda: Customer("CUS-01", "Alice Zhang", "alice@example.com"),
    "Invoice": lambda: Invoice("INV-2026-001", "CUS-01", 259_900, "USD"),
    "Payment": lambda: Payment("PAY-9001", "INV-2026-001", 259_900, "card"),
    "ReportRow": lambda: ReportRow("2026-11", 42, 1_090_000),
}
