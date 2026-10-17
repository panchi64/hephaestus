import unittest

from wordcount import count_words


class CountWordsTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(count_words(""), 0)

    def test_spaces(self):
        self.assertEqual(count_words("one  two\tthree\n"), 3)


if __name__ == "__main__":
    unittest.main()
