""".env loader: parsing rules and precedence (terminal env wins over the file)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.env import load_env  # noqa: E402


class TestEnvLoader(unittest.TestCase):
    KEYS = ["IV_T_PLAIN", "IV_T_QUOTED", "IV_T_EXPORT", "IV_T_COMMENT", "IV_T_EMPTY", "IV_T_KEEP"]

    def setUp(self):
        self.saved = {k: os.environ.pop(k, None) for k in self.KEYS}
        self.path = Path(tempfile.mkdtemp()) / ".env"
        self.path.write_text("\n".join([
            "# comment", "", "IV_T_PLAIN=abc", 'IV_T_QUOTED="x y # not a comment"', "export IV_T_EXPORT=1",
            "IV_T_COMMENT=val  # trailing comment", "IV_T_EMPTY=", "IV_T_KEEP=from-file"]))

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def test_parsing_and_precedence(self):
        os.environ["IV_T_KEEP"] = "from-terminal"
        loaded = load_env(self.path)
        self.assertEqual(os.environ["IV_T_PLAIN"], "abc")
        self.assertEqual(os.environ["IV_T_QUOTED"], "x y # not a comment")
        self.assertEqual(os.environ["IV_T_EXPORT"], "1")
        self.assertEqual(os.environ["IV_T_COMMENT"], "val")
        self.assertNotIn("IV_T_EMPTY", os.environ)            # empty values don't clobber defaults
        self.assertEqual(os.environ["IV_T_KEEP"], "from-terminal")  # terminal wins
        self.assertNotIn("IV_T_KEEP", loaded)

    def test_missing_file(self):
        self.assertEqual(load_env(self.path.parent / "nope.env"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
