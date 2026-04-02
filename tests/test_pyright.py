"""Fail if pyright reports any warnings or errors."""

import subprocess


def test_pyright_clean():
    result = subprocess.run(["pyright"], capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"pyright found issues:\n{result.stdout}")
