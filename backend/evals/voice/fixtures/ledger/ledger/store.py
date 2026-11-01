class Ledger:
    def __init__(self, entries=[]):
        self.entries = entries

    def add(self, name, amount):
        self.entries.append((name, amount))

    def total(self):
        return sum(amount for _, amount in self.entries)
