"""A small, realistic ledger domain model used as the certification target.

Eight classes are deliberately written in the shape slotcert can certify:
one plain ``__init__``, literal ``self.x = ...`` assignments, no inheritance.
Four more classes exercise every rejection path.
"""

from __future__ import annotations


class Money:
    def __init__(self, amount, currency):
        self.amount = amount
        self.currency = currency


class Account:
    def __init__(self, account_id, owner, balance):
        self.account_id = account_id
        self.owner = owner
        self.balance = balance


class Transaction:
    def __init__(self, txid, account_id, amount, memo):
        self.txid = txid
        self.account_id = account_id
        self.amount = amount
        self.memo = memo


class LedgerEntry:
    def __init__(self, seq, txid, debit, credit):
        self.seq = seq
        self.txid = txid
        self.debit = debit
        self.credit = credit


class Customer:
    def __init__(self, customer_id, name, email):
        self.customer_id = customer_id
        self.name = name
        self.email = email


class Invoice:
    def __init__(self, invoice_id, customer_id, total, currency):
        self.invoice_id = invoice_id
        self.customer_id = customer_id
        self.total = total
        self.currency = currency


class Payment:
    def __init__(self, payment_id, invoice_id, amount, method):
        self.payment_id = payment_id
        self.invoice_id = invoice_id
        self.amount = amount
        self.method = method


class ReportRow:
    def __init__(self, key, count, total):
        self.key = key
        self.count = count
        self.total = total


# --- deliberately unsafe shapes -------------------------------------------------

class ConditionalAccount:
    """Cannot be slotted: the attribute set depends on a runtime branch."""

    def __init__(self, account_id, with_owner):
        self.account_id = account_id
        if with_owner:
            self.owner = "unknown"


class DynamicTag:
    """Cannot be slotted: attributes are attached through setattr()."""

    def __init__(self, name, value):
        setattr(self, name, value)


class ChildAccount(Account):
    """Cannot be slotted safely: the base class owns the instance layout."""

    def __init__(self, account_id, owner, balance, tier):
        super().__init__(account_id, owner, balance)
        self.tier = tier


class AlreadySlotted:
    __slots__ = ("code",)

    def __init__(self, code):
        self.code = code
