import unittest

from checkout import total


class CheckoutTests(unittest.TestCase):
    def test_total_includes_tax(self):
        self.assertEqual(total([100], tax_rate=0.10), 110.0)


if __name__ == "__main__":
    unittest.main()
