"""A small stock ledger."""

from dataclasses import dataclass


@dataclass
class Item:
    sku: str
    name: str
    quantity: int
    unit_price_cents: int


def total_value_cents(items):
    return sum(item.quantity * item.unit_price_cents for item in items)


def restock(items, sku, amount):
    for item in items:
        if item.sku == sku:
            item.quantity += amount
            return item
    raise KeyError(sku)


def low_stock(items, threshold=5):
    return [item for item in items if item.quantity < threshold]


def parse_line(line):
    sku, name, quantity, price = line.split(",")
    return Item(sku.strip(), name.strip(), int(quantity), int(float(price) * 100))


def load(path):
    with open(path) as handle:
        return [parse_line(line) for line in handle if line.strip()]
