"""``uploader/api/**`` must never import ``uploader/devblob/**``.

A router mounted inside ``app.py`` behind ``if config.issuer == 'local'``
is one refactor away from being reachable in production; a module the
production app never imports is not (§1 of
``uploader/spec/tasks/06-mock-azure.md``). This test is what keeps that
property true as the codebase changes, rather than trusting a code review
to notice a stray import.
"""

import ast
import unittest
from pathlib import Path

API_DIR = Path(__file__).resolve().parent.parent


def _imported_names(path: Path) -> set:
    """Return every module name imported by the source file at ``path``."""
    tree = ast.parse(path.read_text(), filename=str(path))
    names = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)

    return names


class TestApiNeverImportsDevblob(unittest.TestCase):

    def test_no_source_file_under_api_imports_devblob(self):
        offenders = []
        for path in API_DIR.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            for name in _imported_names(path):
                if name == "devblob" or name.startswith("devblob."):
                    offenders.append((str(path), name))

        self.assertEqual(
            offenders, [],
            f"uploader/api/** must never import uploader/devblob/**, "
            f"found: {offenders}")


if __name__ == "__main__":
    unittest.main()
