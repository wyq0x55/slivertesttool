"""Formula-injection, control-char, filename and bounded-regex tests."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from lm_helpers import load  # noqa: E402

lm = load()
sec = lm.security


class FormulaInjectionTest(unittest.TestCase):
    def test_detects_prefixes(self):
        for v in ("=1+1", "+A1", "-2", "@SUM", "\tx", "\rx"):
            self.assertTrue(sec.is_formula_like(v), v)
        self.assertFalse(sec.is_formula_like("hello"))

    def test_escape_on_export(self):
        self.assertEqual(sec.escape_formula("=cmd"), "'=cmd")
        self.assertEqual(sec.escape_formula("plain"), "plain")
        self.assertEqual(sec.escape_formula(42), 42)

    def test_sanitize_strips_control_chars(self):
        self.assertEqual(sec.sanitize_incoming("a\x07b"), "ab")
        # tab/newline kept for multiline text
        self.assertEqual(sec.sanitize_incoming("a\tb\nc"), "a\tb\nc")

    def test_has_control_chars(self):
        self.assertTrue(sec.has_control_chars("x\x01"))
        self.assertFalse(sec.has_control_chars("x\ty"))


class FilenameTest(unittest.TestCase):
    def test_strips_path_and_unsafe(self):
        self.assertEqual(sec.safe_filename("../../etc/passwd"), "passwd")
        self.assertNotIn("/", sec.safe_filename("a/b/c.xlsx"))

    def test_keeps_chinese(self):
        self.assertEqual(sec.safe_filename("测试.xlsx"), "测试.xlsx")

    def test_default_when_empty(self):
        self.assertEqual(sec.safe_filename("   "), "file")


class RegexBoundTest(unittest.TestCase):
    def test_length_cap(self):
        with self.assertRaises(sec.UnsafeRegexError):
            sec.compile_user_regex("a" * (sec.MAX_REGEX_LEN + 1))

    def test_invalid_regex(self):
        with self.assertRaises(sec.UnsafeRegexError):
            sec.compile_user_regex("(")

    def test_valid_compiles(self):
        rx = sec.compile_user_regex(r"\d+")
        self.assertTrue(rx.search("abc123"))

    def test_match_with_timeout_returns(self):
        rx = sec.compile_user_regex("foo")
        self.assertIsNotNone(sec.match_with_timeout(rx, "a foo b"))


if __name__ == "__main__":
    unittest.main()
