from inventory import Item, low_stock, parse_line, restock, total_value_cents


def _items():
    return [Item("A1", "Widget", 3, 250), Item("B2", "Gadget", 10, 1000)]


def test_total_value():
    assert total_value_cents(_items()) == 3 * 250 + 10 * 1000


def test_restock_adds_to_the_matching_item():
    items = _items()
    assert restock(items, "A1", 4).quantity == 7


def test_low_stock_uses_the_threshold():
    assert [item.sku for item in low_stock(_items())] == ["A1"]


def test_parse_line_reads_the_price_in_cents():
    assert parse_line("C3, Sprocket, 2, 19.99").unit_price_cents == 1999


def test_parse_line_reads_a_round_price():
    assert parse_line("D4, Bolt, 100, 0.50").unit_price_cents == 50
