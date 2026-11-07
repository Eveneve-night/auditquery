"""Offline product regression suite. Historical report checks remain separate.

Run from the repository root: python tools/test_product.py
"""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

# Explicit scope: identity, credentials, lifecycle, SQL safety, evidence and budgets.
PATTERNS = (
    "test_llm_credentials.py", "test_llm_gateway.py", "test_loop_guard.py",
    "test_grounded_answer_contract.py", "test_rag_evidence_grounding.py",
    "test_d4_sql_safety.py", "test_d5_auth_rbac.py", "test_d5_secret_store.py",
    "test_d5_document_lifecycle.py", "test_d5_product_server.py",
    "test_d5_product_snapshot.py", "test_data_source_contracts.py",
    "test_fin_source_registry.py", "test_fin_source_storage.py",
)

if __name__ == "__main__":
    suite = unittest.TestSuite()
    for pattern in PATTERNS:
        suite.addTests(unittest.defaultTestLoader.discover(str(ROOT / "tests/unit"), pattern=pattern))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
