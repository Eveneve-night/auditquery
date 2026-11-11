"""Specialized offline suites that are not part of the core product regression.

Core product regression entry point: tools/test_product.py

Run from the repository root:
    python tools/test_specialized.py --list
    python tools/test_specialized.py
    python tools/test_specialized.py --suite frontend-console

Exit codes:
    0  every selected suite ran and passed
    1  at least one selected suite failed, or a pattern matched no file
    2  usage error (unknown suite name, no suites selected)
    3  at least one selected suite was skipped for a missing prerequisite

Suites are declared in SUITES. Each pattern must match exactly one file under
tests/unit. A pattern that stops matching is treated as a failure rather than a
silent reduction in coverage.
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests/unit"

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_INCOMPLETE = 3

SUITES: dict[str, dict[str, object]] = {
    "round2-document": {
        "description": "Document layout parsing, complexity tiers and layout reuse.",
        "patterns": (
            "test_round2_document_complexity.py",
            "test_round2_document_layout_reuse.py",
        ),
        "requires": ("pdfplumber",),
    },
    "round2-schema-linking": {
        "description": "Round 2 schema linking over the public case set.",
        "patterns": (
            "test_round2_schema_linking.py",
            "test_round2_schema_linking_public.py",
        ),
        "requires": (),
    },
    "frontend-console": {
        "description": "Console adapter, live wiring and install/restart probes.",
        "patterns": (
            "test_d5_frontend_console.py",
            "test_d5_frontend_live_wiring.py",
            "test_d5_frontend_install_restart_probe.py",
        ),
        "requires": ("node",),
    },
    "fin-frontend": {
        "description": "Data source management components and public business fixtures.",
        "patterns": (
            "test_fin_c_frontend.py",
            "test_fin_c_public_business.py",
            "test_fin_same_repo_tools.py",
        ),
        "requires": ("node",),
    },
    "visitor-rag": {
        "description": "Visitor session, RAG dispatcher, published retriever and answer composition.",
        "patterns": (
            "test_d5_visitor.py",
            "test_d5_visitor_rag_dispatcher.py",
            "test_d5_rag_published_retriever.py",
            "test_d5_rag_answer_composition.py",
        ),
        "requires": (),
    },
}

KNOWN_REQUIREMENTS = ("pdfplumber", "node", "playwright")


def all_patterns() -> list[str]:
    """Return every declared pattern across suites, in declaration order."""
    patterns: list[str] = []
    for spec in SUITES.values():
        patterns.extend(spec["patterns"])  # type: ignore[arg-type]
    return patterns


def resolve_pattern(pattern: str) -> list[Path]:
    """Return the files matched by a pattern inside tests/unit."""
    return sorted(TESTS_DIR.glob(pattern))


def missing_requirements(suite: str) -> list[str]:
    """Return prerequisites of a suite that are not available locally."""
    spec = SUITES[suite]
    missing: list[str] = []
    for requirement in spec["requires"]:  # type: ignore[union-attr]
        if requirement == "node":
            if shutil.which("node") is None:
                missing.append("node")
        elif requirement == "playwright":
            try:
                if importlib.util.find_spec("playwright") is None:
                    missing.append("playwright")
            except (ImportError, ValueError):
                missing.append("playwright")
        else:
            try:
                if importlib.util.find_spec(requirement) is None:
                    missing.append(requirement)
            except (ImportError, ValueError):
                missing.append(requirement)
    return missing


def build_suite(suite: str) -> unittest.TestSuite:
    """Collect one declared suite. Callers must check pattern resolution first."""
    collected = unittest.TestSuite()
    loader = unittest.defaultTestLoader
    for pattern in SUITES[suite]["patterns"]:  # type: ignore[union-attr]
        collected.addTests(loader.discover(str(TESTS_DIR), pattern=pattern))
    return collected


def describe(selected: list[str]) -> None:
    """Print the declared suites without executing them."""
    print("Declared specialized suites:")
    for name in selected:
        spec = SUITES[name]
        requires = ", ".join(spec["requires"]) or "none"  # type: ignore[arg-type]
        print(f"  {name}")
        print(f"    description : {spec['description']}")
        print(f"    requires    : {requires}")
        for pattern in spec["patterns"]:  # type: ignore[union-attr]
            matched = len(resolve_pattern(pattern))
            print(f"    pattern     : {pattern} ({matched} file)")
    print("")
    print("Core product regression stays at: python tools/test_product.py")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run specialized offline test suites declared in tools/test_specialized.py.",
    )
    parser.add_argument(
        "--suite",
        action="append",
        choices=sorted(SUITES),
        help="Run only the named suite. Repeat to select several. Defaults to all suites.",
    )
    parser.add_argument("--list", action="store_true", help="List declared suites and exit.")
    parser.add_argument("--verbose", action="store_true", help="Use verbose unittest output.")
    parser.add_argument(
        "--allow-skip",
        action="store_true",
        help="Treat suites skipped for missing prerequisites as acceptable (exit 0).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    selected = list(dict.fromkeys(args.suite or SUITES.keys()))

    if args.list:
        describe(selected)
        return EXIT_OK

    unresolved = [
        (suite, pattern)
        for suite in selected
        for pattern in SUITES[suite]["patterns"]  # type: ignore[union-attr]
        if not resolve_pattern(pattern)
    ]
    if unresolved:
        print("Declared patterns that match no file under tests/unit:", file=sys.stdout)
        for suite, pattern in unresolved:
            print(f"  {suite}: {pattern}", file=sys.stdout)
        print("Coverage would silently shrink. Fix the pattern before running.", file=sys.stdout)
        return EXIT_FAILED

    verbosity = 2 if args.verbose else 1
    outcomes: list[tuple[str, str, int, int, int, int]] = []
    skipped_suites: list[tuple[str, str]] = []

    for suite in selected:
        missing = missing_requirements(suite)
        if missing:
            reason = "missing prerequisite: " + ", ".join(missing)
            print(f"\n=== {suite} === SKIPPED ({reason})")
            outcomes.append((suite, "skipped", 0, 0, 0, 0))
            skipped_suites.append((suite, reason))
            continue

        print(f"\n=== {suite} ===")
        result = unittest.TextTestRunner(verbosity=verbosity, stream=sys.stdout).run(build_suite(suite))
        status = "passed" if result.wasSuccessful() else "failed"
        outcomes.append(
            (suite, status, result.testsRun, len(result.failures), len(result.errors), len(result.skipped))
        )

    print("")
    print("Summary")
    print(f"  {'suite':<24}{'status':<10}{'ran':>5}{'fail':>6}{'error':>7}{'skip':>6}")
    for suite, status, ran, failures, errors, skips in outcomes:
        print(f"  {suite:<24}{status:<10}{ran:>5}{failures:>6}{errors:>7}{skips:>6}")

    total_ran = sum(item[2] for item in outcomes)
    failed = [item[0] for item in outcomes if item[1] == "failed"]
    if failed:
        print("")
        print("Failed suites: " + ", ".join(failed))
        return EXIT_FAILED

    if skipped_suites:
        print("")
        print(f"{len(skipped_suites)} suite(s) did not run, so this result is incomplete:")
        for suite, reason in skipped_suites:
            print(f"  {suite}: {reason}")
        if args.allow_skip:
            print("--allow-skip set; reporting success for the executed subset only.")
            return EXIT_OK
        print("Install requirements-dev.txt for pdfplumber and Node.js for the probes, then rerun.")
        return EXIT_INCOMPLETE

    print("")
    print(f"All {len(outcomes)} suite(s) passed, {total_ran} tests.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
