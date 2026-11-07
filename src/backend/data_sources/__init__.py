"""Data-source contracts; implementations belong to explicit adapters."""
from .contracts import DataSourceContractError, validate_source, validate_connector, validate_snapshot, validate_transition
