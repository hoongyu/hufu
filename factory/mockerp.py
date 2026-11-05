"""In-memory stand-in for a factory ERP. The approved-supplier list lives in the mandate; this
holds inventory, purchase orders, payables, quality holds and the purchasing mailbox."""

from collections.abc import Callable
from dataclasses import dataclass, field

from engine.money import format_minor


@dataclass(frozen=True)
class Email:
    sender: str
    subject: str
    body: str


@dataclass(frozen=True)
class StockItem:
    sku: str
    name: str
    on_hand: int
    safety_stock: int
    unit: str
    supplier_id: str
    unit_price: str  # CNY, major units, as quoted in the frame contract


@dataclass(frozen=True)
class PurchaseOrder:
    po_id: str
    supplier: str
    item: str
    qty: int
    amount_minor: int
    currency: str


@dataclass
class Invoice:
    invoice_id: str
    supplier_id: str
    amount_minor: int
    currency: str
    paid: bool = False


@dataclass(frozen=True)
class Payment:
    invoice_id: str
    to_account: str
    to_name: str
    amount_minor: int
    currency: str


@dataclass
class QualityHold:
    lot_id: str
    item: str
    reason: str
    released: bool = False


@dataclass
class MockERP:
    company: str
    inventory: list[StockItem]
    invoices: dict[str, Invoice]
    holds: dict[str, QualityHold]
    inbox: list[Email] = field(default_factory=list)
    orders: list[PurchaseOrder] = field(default_factory=list)
    payments: list[Payment] = field(default_factory=list)
    # a counterparty check run before every action that changes something (factory/identity.py); None = off
    gate: Callable[[str], None] | None = None

    def _admit(self, action: str) -> None:
        if self.gate is not None:
            self.gate(action)

    def create_po(
        self, supplier: str, item: str, qty: int, amount_minor: int, currency: str
    ) -> PurchaseOrder:
        if qty <= 0 or amount_minor <= 0:
            raise ValueError("quantity and amount must be positive")
        self._admit("create_po")
        po = PurchaseOrder(
            f"PO-{len(self.orders) + 1:04d}", supplier, item, qty, amount_minor, currency
        )
        self.orders.append(po)
        return po

    def pay(self, invoice_id: str, to_account: str, to_name: str) -> Payment:
        invoice = self.invoice(invoice_id)
        if invoice.paid:
            raise ValueError(f"invoice {invoice_id} is already paid")
        self._admit("pay_invoice")
        invoice.paid = True
        done = Payment(invoice_id, to_account, to_name, invoice.amount_minor, invoice.currency)
        self.payments.append(done)
        return done

    def release(self, lot_id: str) -> QualityHold:
        hold = self.holds.get(lot_id)
        if hold is None:
            raise ValueError(f"unknown lot {lot_id!r}")
        if hold.released:
            raise ValueError(f"lot {lot_id} is already released")
        self._admit("release_quality_hold")
        hold.released = True
        return hold

    def invoice(self, invoice_id: str) -> Invoice:
        invoice = self.invoices.get(invoice_id)
        if invoice is None:
            raise ValueError(f"unknown invoice {invoice_id!r}")
        return invoice

    def contract_price(self, supplier_id: str, item: str) -> str | None:
        """Unit price of this item from this supplier in the frame contract, or None if it has none."""
        return next(
            (
                s.unit_price
                for s in self.inventory
                if s.supplier_id == supplier_id and s.name == item
            ),
            None,
        )

    def spent(self, currency: str = "CNY") -> str:
        return format_minor(
            sum(o.amount_minor for o in self.orders if o.currency == currency), currency
        )
