"""Print the stock's total value."""

from inventory import load, total_value_cents

print(f"Total stock value: {total_value_cents(load('stock.csv'))} cents")
