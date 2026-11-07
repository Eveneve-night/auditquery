"""Compile-time whitelist: descriptors do not grant database execution."""
from copy import deepcopy
from ..product.secret_store import SecretStoreError


class ConnectorRegistry:
    def describe(self,connector):
        if connector!={'id':'sqlite','version':'1.0.0'}:
            raise SecretStoreError('invalid_request')
        # No controlled SQLite adapter is implemented in FIN-DS02/03/04.
        return {'contract_version':'1.0.0','connector':deepcopy(connector),
                'capabilities':{key:('not_applicable' if key=='tls' else 'unsupported')
                                for key in ('readonly','single_table','timeout','cancel','pooling','tls','network','multi_table')}}

    def require(self,connector,operation):
        from .contracts import DataSourceContractError
        self.describe(connector)
        # Test/sync/execute need a real controlled adapter; no dynamic imports,
        # caller paths, mock successes or capability upgrades are supported.
        raise DataSourceContractError('required_capability_unavailable')
