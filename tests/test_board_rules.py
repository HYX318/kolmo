import unittest

from kolmo.ashare.board_rules import board_for_symbol


class BoardRulesTest(unittest.TestCase):
    def test_sz_boards(self) -> None:
        self.assertEqual(board_for_symbol("000001.SZ"), "main")
        self.assertEqual(board_for_symbol("002415.SZ"), "sme")
        self.assertEqual(board_for_symbol("300750.SZ"), "chi_next")
        self.assertEqual(board_for_symbol("301001.SZ"), "chi_next")

    def test_sh_boards(self) -> None:
        self.assertEqual(board_for_symbol("600000.SH"), "main")
        self.assertEqual(board_for_symbol("601398.SH"), "main")
        self.assertEqual(board_for_symbol("688981.SH"), "star")


if __name__ == "__main__":
    unittest.main()
