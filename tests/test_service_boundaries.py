"""BOOK-64 step 5b-3, BOOK-65: the boundaries between Booking, Notification and
Payment, enforced.

Separate services don't import each other's code -- they talk through events
(and share only the event contract). This test reads every Python file of
each service and fails if one imports the other, so the boundary is checked
on every run instead of just promised (microservices-basics.md §4: a shared
import is how a "distributed monolith" starts).

It parses the source (ast), never imports it -- so it needs no Docker, no
settings, and catches an import even inside a function body.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# service -> (where its code lives, top-level packages it must never import)
BOUNDARIES = {
    "Booking": (REPO_ROOT / "app", {"notification", "payment"}),
    "Notification": (REPO_ROOT / "services" / "notification" / "src", {"app", "payment"}),
    # BOOK-65: Payment talks to Booking over HTTP and Kafka only
    "Payment": (REPO_ROOT / "services" / "payment" / "src", {"app", "notification"}),
}


def _imported_top_levels(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def _violations(root: Path, forbidden: set[str]) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.py")):
        imported = _imported_top_levels(path.read_text(encoding="utf-8"))
        for name in sorted(imported & forbidden):
            found.append(f"{path.relative_to(REPO_ROOT)} imports {name!r}")
    return found


@pytest.mark.parametrize("service", BOUNDARIES)
def test_service_does_not_import_the_other(service: str) -> None:
    root, forbidden = BOUNDARIES[service]
    assert root.is_dir(), f"{service}'s code not found at {root}"

    assert _violations(root, forbidden) == []


# the checker itself has to be able to see a violation -- including the
# `from x import y` and in-function forms -- or the tests above prove nothing
def test_checker_spots_every_import_form() -> None:
    source = (
        "import notification\n"
        "from notification.consumer import run\n"
        "def f():\n"
        "    import app.core.config\n"
        "from . import sibling\n"  # relative: never another service
    )
    assert _imported_top_levels(source) == {"notification", "app"}
