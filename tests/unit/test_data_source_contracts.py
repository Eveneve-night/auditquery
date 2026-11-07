from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from backend.data_sources.contracts import (
    DataSourceContractError, validate_source, validate_connector, validate_snapshot, validate_transition,
)


def examples():
    binding = {'schema_sha256': 'a'*64, 'authorized_object_ids': ['table.artist'],
               'domain_pack': {'id': 'chinook', 'version': '1.0.0', 'manifest_sha256': 'b'*64},
               'policy': {'version': '1.0.0', 'sha256': 'c'*64}}
    source = {'contract_version': '1.0.0', 'source_id': 'source.demo', 'revision': 3,
              'status': 'published', 'connector': {'id': 'sqlite', 'version': '1.0.0'},
              'sqlite_asset_id': 'asset.synthetic', 'credential_ref': None, 'binding': binding}
    descriptor = {'contract_version': '1.0.0', 'connector': deepcopy(source['connector']),
                  'capabilities': {key: ('supported' if key in {'readonly', 'single_table', 'timeout'} else
                                        'not_applicable' if key == 'tls' else 'unsupported')
                                   for key in ('readonly','single_table','timeout','cancel','pooling','tls','network','multi_table')}}
    snapshot = {'contract_version': '1.0.0', 'source_id': source['source_id'], 'source_revision': 3,
                'connector': deepcopy(source['connector']), 'binding': deepcopy(binding)}
    return source, descriptor, snapshot


class DataSourceContractsTest(unittest.TestCase):
    def test_valid_contracts_and_snapshot(self):
        s,c,q=examples(); before=deepcopy((s,c,q))
        validate_source(s); validate_connector(c)
        self.assertIsNone(validate_snapshot(q,s,authorized_source_ids=['source.demo'],descriptor=c))
        self.assertEqual(before,(s,c,q))

    def test_unknown_fields_and_paths_and_connectors_fail_closed(self):
        for key,value in [('password','fake'),('dsn','sqlite:///tmp/x'),('expected',{}),
                          ('sqlite_asset_id','../escape'),('sqlite_asset_id','C:\\outside'),
                          ('connector',{'id':'postgresql','version':'1.0.0'})]:
            s,_,_=examples();s[key]=value
            with self.subTest(key=key),self.assertRaises(DataSourceContractError):validate_source(s)

    def test_credential_scope_and_no_binding(self):
        s,_,_=examples();s['credential_ref']={'secret_id':'opaque.test','source_id':'other','purpose':'data_source','revision':1}
        with self.assertRaisesRegex(DataSourceContractError,'credential_scope_mismatch'):validate_source(s)
        s['credential_ref']=None;s['binding']=None
        with self.assertRaisesRegex(DataSourceContractError,'binding_required'):validate_source(s)

    def test_snapshot_stale_and_unauthorized_and_disabled(self):
        for field,value in [('source_revision',2),('binding',{**examples()[0]['binding'],'schema_sha256':'d'*64})]:
            s,c,q=examples();q[field]=value
            with self.assertRaisesRegex(DataSourceContractError,'stale_source_binding'):
                validate_snapshot(q,s,authorized_source_ids=['source.demo'],descriptor=c)
        s,c,q=examples()
        with self.assertRaisesRegex(DataSourceContractError,'source_not_found'):
            validate_snapshot(q,s,authorized_source_ids=[],descriptor=c)
        for status in ('draft','verified','synced','draining','disabled','deleted'):
            s['status']=status
            with self.assertRaisesRegex(DataSourceContractError,'source_not_available'):
                validate_snapshot(q,s,authorized_source_ids=['source.demo'],descriptor=c)

    def test_zero_capabilities_and_unsupported_values(self):
        for key in ('readonly','single_table','timeout'):
            s,c,q=examples();c['capabilities'][key]='unsupported'
            with self.assertRaisesRegex(DataSourceContractError,'required_capability_unavailable'):
                validate_snapshot(q,s,authorized_source_ids=['source.demo'],descriptor=c)
        for key,value in [('network','supported'),('multi_table','supported'),('tls','supported'),('cancel','magic')]:
            _,c,_=examples();c['capabilities'][key]=value
            with self.assertRaises(DataSourceContractError):validate_connector(c)

    def test_lifecycle_preconditions_no_test_to_publish_shortcut(self):
        s,_,_=examples();s['status']='draft';s['binding']=None
        validate_transition(s,'verified',expected_revision=3,actor_role='admin',operation_id='op.test')
        with self.assertRaisesRegex(DataSourceContractError,'invalid_transition'):
            validate_transition(s,'published',expected_revision=3,actor_role='admin',operation_id='op.test')
        s['status']='published';s['binding']=examples()[0]['binding']
        validate_transition(s,'draining',expected_revision=3,actor_role='admin',operation_id='op.drain')
        s['status']='draining'
        with self.assertRaisesRegex(DataSourceContractError,'source_busy'):
            validate_transition(s,'disabled',expected_revision=3,actor_role='admin',operation_id='op.disable',inflight=1)

    def test_auth_revision_and_operation_checks(self):
        s,_,_=examples()
        for kwargs,code in [({'actor_role':'visitor'},'forbidden'),({'expected_revision':2},'revision_conflict'),
                            ({'operation_id':''},'invalid_operation_id'),({'inflight':-1},'invalid_inflight_count')]:
            args=dict(expected_revision=3,actor_role='admin',operation_id='op.drain');args.update(kwargs)
            with self.assertRaisesRegex(DataSourceContractError,code):validate_transition(s,'draining',**args)


if __name__=='__main__':unittest.main()
