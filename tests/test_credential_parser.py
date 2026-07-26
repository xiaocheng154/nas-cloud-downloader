from __future__ import annotations

import unittest

from credential_parser import extract_baidu_credentials, normalize_cookie


class CredentialParserTests(unittest.TestCase):
    def test_normalize_cookie_removes_prefix_invalid_empty_and_duplicates(self) -> None:
        raw = " Cookie: a=1; broken; a=2;\r\n b=base==; empty= ; =value "

        self.assertEqual(normalize_cookie(raw), "a=1; b=base==")

    def test_extract_baidu_credentials_keeps_only_bduss_and_stoken(self) -> None:
        raw = (
            "Cookie: BAIDUID=ignored; BDUSS=bduss-value; "
            "STOKEN=stoken-value; other=ignored"
        )

        self.assertEqual(
            extract_baidu_credentials(raw),
            {"bduss": "bduss-value", "stoken": "stoken-value"},
        )

    def test_extract_baidu_credentials_is_case_insensitive(self) -> None:
        self.assertEqual(
            extract_baidu_credentials("bduss=one; stoken=two"),
            {"bduss": "one", "stoken": "two"},
        )


if __name__ == "__main__":
    unittest.main()
