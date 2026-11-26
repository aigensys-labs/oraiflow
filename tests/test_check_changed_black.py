"""Regression tests for incremental Black enforcement in GitHub Actions."""

from scripts.check_changed_black import _changed_line_ranges


def test_changed_line_ranges_expand_context_and_merge_adjacent_hunks():
    diff = """diff --git a/services/example.py b/services/example.py
index 1111111..2222222 100644
--- a/services/example.py
+++ b/services/example.py
@@ -10,2 +11,3 @@ def example():
@@ -15 +15,2 @@ def example():
"""

    assert _changed_line_ranges(diff) == {"services/example.py": [(10, 17)]}


def test_changed_line_ranges_ignore_deletion_only_hunks():
    diff = """diff --git a/services/example.py b/services/example.py
index 1111111..2222222 100644
--- a/services/example.py
+++ b/services/example.py
@@ -10,2 +9,0 @@ def example():
"""

    assert _changed_line_ranges(diff) == {}


def test_changed_line_ranges_keep_separate_python_files_separate():
    diff = """diff --git a/services/one.py b/services/one.py
--- a/services/one.py
+++ b/services/one.py
@@ -2 +2,2 @@
diff --git a/services/two.py b/services/two.py
--- a/services/two.py
+++ b/services/two.py
@@ -8 +8,2 @@
"""

    assert _changed_line_ranges(diff) == {
        "services/one.py": [(1, 4)],
        "services/two.py": [(7, 10)],
    }
