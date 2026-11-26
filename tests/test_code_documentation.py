"""Regression tests for production Python documentation coverage."""

from scripts.check_docstrings import find_missing_docstrings


def test_production_modules_and_public_interfaces_have_docstrings():
    assert find_missing_docstrings() == []
