import unittest

from ledger import Ledger


class LedgerTest(unittest.TestCase):
    def test_add_one(self):
        ledger = Ledger()
        ledger.add("coffee", 4)
        self.assertEqual(ledger.total(), 4)

    def test_add_two(self):
        ledger = Ledger()
        ledger.add("rent", 900)
        ledger.add("power", 60)
        self.assertEqual(ledger.total(), 960)

    def test_starts_empty(self):
        self.assertEqual(Ledger().entries, [])


if __name__ == "__main__":
    unittest.main()
