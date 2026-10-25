import os
import tempfile
import unittest


_test_database = tempfile.TemporaryDirectory()
os.environ["CROSSLINE_DB"] = os.path.join(_test_database.name, "universe-test.sqlite3")

from backend.main import parse_nifty100_members


class ParseNifty100MembersTests(unittest.TestCase):
    def test_parses_nse_archive_csv_columns(self):
        csv_text = "Company Name,Industry,Symbol,Series,ISIN Code\n" + "".join(
            f"Company {index},Industry,STOCK{index:02},EQ,ISIN{index:02}\n"
            for index in range(100)
        )

        members = parse_nifty100_members(csv_text)

        self.assertEqual(len(members), 100)
        self.assertEqual(members["STOCK00"], "Company 0")

    def test_rejects_redirect_html(self):
        with self.assertRaisesRegex(ValueError, "Symbol column"):
            parse_nifty100_members("<html><title>Not Found</title></html>")

    def test_rejects_truncated_constituent_lists(self):
        with self.assertRaisesRegex(ValueError, "expected a Nifty 100 list"):
            parse_nifty100_members("Symbol,Company Name\nABC,Example Co\n")


if __name__ == "__main__":
    unittest.main()