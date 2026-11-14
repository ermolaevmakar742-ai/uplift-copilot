"""Static regression checks on the live pipeline file. main.py is never imported (it opens the Telegram session)."""
import ast
import glob
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAIN = os.path.join(ROOT, "main.py")


def src():
    with open(MAIN, encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    unittest.main()
