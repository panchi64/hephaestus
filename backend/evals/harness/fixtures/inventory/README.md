# inventory

A small stock ledger: items with a SKU, a name, a quantity and a unit price in cents.

- `total_value_cents(items)` is the stock's total value.
- `restock(items, sku, amount)` adds stock to one item.
- `low_stock(items, threshold=5)` returns the items at or below the threshold.
- `parse_line(line)` reads one `sku, name, quantity, price` line, price in dollars.
- `load(path)` reads a whole file of them.

Run the tests with `python -m unittest discover tests`.
