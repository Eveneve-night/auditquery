"""NL2SQL module: produce schema-validated candidate SQL without executing it."""

from .contract import (
    NL2SQL_OUTPUT_SCHEMA_ID,
    NL2SQL_PROMPT_VERSION,
    NL2SQLRequest,
    NL2SQLResult,
    NL2SQLSchemaValidationError,
    validate_model_output,
)

__all__ = [
    "NL2SQLRequest",
    "NL2SQLResult",
    "NL2SQL_OUTPUT_SCHEMA_ID",
    "NL2SQL_PROMPT_VERSION",
    "NL2SQLSchemaValidationError",
    "validate_model_output",
]
"""NL2SQL package.

Runtime adapters are imported from their concrete modules to avoid making the
package initializer part of the D4-05 recovery dependency graph.
"""
