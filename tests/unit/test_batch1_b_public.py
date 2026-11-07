"""Real #104/#102 in-process integration, no fixture result substitution."""
import importlib.util
import os
import sys
from pathlib import Path
from copy import deepcopy
import unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'))
from backend.agent.domain_pack import DomainPackRegistry
from backend.semantics.public import PublicSemanticAdapter


def contract_module():
    root=Path(os.environ.get('BATCH1_A_ROOT',ROOT))
    path=root/'tools/evaluation/validate_batch1_semantic_contracts.py'
    if not path.is_file():
        raise unittest.SkipTest('Requires published A #104 dependency; set BATCH1_A_ROOT for isolated integration')
    spec=importlib.util.spec_from_file_location('batch1_public_contract_dependency',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def public_request(pack, question, **session):
    context=pack.model_context(max_data_level='L1')
    fields={'concepts':'concept_id','slots':'slot_id','schema_catalog':'object_id'}
    ids=[item[fields[role]] for role,artifact in context['artifacts'].items() if role in fields for item in artifact['items']]
    return dict(request_version='1.0.0',request_id='request.public',question=question,
                domain_context={'domain_pack_ref':{'domain_pack_id':pack.domain_pack_id,'version':pack.version,'manifest_sha256':pack.manifest_sha256},
                                'authorized_catalog':{'candidate_ids':ids[:200],'max_data_level':'L1'}},
                session=dict(session_id='session.public',turn_index=0,slot_observations=[],**session))


class PublicAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator=contract_module()
        cls.pack=DomainPackRegistry(ROOT/'domain-packs').load_path(ROOT/'domain-packs/chinook/v1')

    def adapter(self): return PublicSemanticAdapter(self.pack,contract_validator=self.validator)

    def test_unicode_actual_public_output_and_reconstruction(self):
        request=public_request(self.pack,'😀𠮷歌手 不要超过20万元')
        result=self.adapter().analyze(request)
        self.validator.validate_contract_pair(request,result,self.pack)
        self.assertIn('不要超过20万元',result['rewritten_question'])
        found=next(f for f in result['fragments'] if f['text']=='歌手')
        self.assertEqual(2,found['start'])
        self.assertEqual('艺术家',found['normalized_value'])
        self.assertFalse(result['sql_execution_authorized'])

    def test_strict_input_denies_answer_slots_and_catalog_escape(self):
        for field in ('required_slots','expected','gold','label'):
            request=public_request(self.pack,'歌手');request[field]=[]
            with self.assertRaises(self.validator.SemanticContractError): self.adapter().analyze(request)
        request=public_request(self.pack,'歌手')
        request['domain_context']['authorized_catalog']['candidate_ids']=['alias.artist_zh_2']
        with self.assertRaises(self.validator.SemanticContractError): self.adapter().analyze(request)

    def test_restricted_catalog_never_rewrites_unauthorized_entity(self):
        request=public_request(self.pack,'歌手')
        request['domain_context']['authorized_catalog']['candidate_ids']=['concept.album']
        result=self.adapter().analyze(request)
        self.assertEqual('歌手',result['rewritten_question'])
        self.assertEqual([],result['edits'])
        self.assertTrue(all(f['status']=='unknown' for f in result['fragments']))

    def test_production_boundary_validator_is_executed(self):
        from unittest.mock import patch
        with patch.object(self.validator,'validate_contract_pair',side_effect=self.validator.SemanticContractError('mutation_rejected')) as check:
            with self.assertRaisesRegex(self.validator.SemanticContractError,'mutation_rejected'):
                self.adapter().analyze(public_request(self.pack,'歌手'))
            check.assert_called_once()

    def test_actual_output_mutations_fail_closed(self):
        request=public_request(self.pack,'😀歌手')
        result=self.adapter().analyze(request)
        for mutation in ('overlap','bounds','unauthorized','evidence','rebuild'):
            r=deepcopy(result)
            if mutation=='overlap': r['edits'].append(dict(r['edits'][0],edit_id='edit.other'))
            elif mutation=='bounds': r['edits'][0]['end']=100
            elif mutation=='unauthorized': r['candidates'][0]['candidate_id']='concept.fake'
            elif mutation=='evidence': r['candidates'][0]['evidence_refs']=['evidence.fake']
            else: r['rewritten_question']='无依据的新问题'
            with self.assertRaises(self.validator.SemanticContractError): self.validator.validate_contract_pair(request,r,self.pack)

    def test_unknown_spelling_and_stable_fragments(self):
        request=public_request(self.pack,'Albm')
        result=self.adapter().analyze(request)
        self.assertEqual('Albm',result['rewritten_question'])
        self.assertIn('unconfirmed_spelling',[i['kind'] for i in result['issues']])
        self.assertEqual(result,self.adapter().analyze(request))

class PublicRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator=contract_module()
        cls.pack=DomainPackRegistry(ROOT/'domain-packs').load_path(ROOT/'domain-packs/chinook/v1')

    def adapter(self): return PublicSemanticAdapter(self.pack,contract_validator=self.validator)

    def test_public_request_to_single_question_redecision_and_original_executor(self):
        from backend.semantics.recovery import SemanticRecovery
        from backend.agent.clarification_recovery import SqlProposal
        bridge=SemanticRecovery(self.adapter())
        req=public_request(self.pack,'艺术家')
        bridge.start(req,recorded_at='2026-10-23T10:00:00Z')
        sid=req['session']['session_id'];q=bridge.pending(sid)
        self.assertEqual('slot.metric',q['slot_id'])
        option=next(x for x in q['options'] if x['value']=='count')
        answer={k:q[k] for k in ('question_id','session_id','request_id','turn_id','state_revision','slot_id')}
        answer.update(answer_id='answer.public',values=['count'],evidence_refs=[option['evidence_ref']])
        wrong=deepcopy(answer);wrong['state_revision']+=1
        with self.assertRaises(Exception): bridge.resume(sid,wrong,recorded_at='2026-10-23T10:01:00Z')
        self.assertEqual(q,bridge.pending(sid))
        bridge.resume(sid,answer,recorded_at='2026-10-23T10:01:00Z')
        self.assertIsNone(bridge.pending(sid))
        with self.assertRaises(Exception): bridge.resume(sid,answer,recorded_at='2026-10-23T10:02:00Z')
        class Port:
            def propose(self,**kwargs): return SqlProposal('proposal.public','SELECT COUNT(*) FROM Artist',('Artist',))
        result=bridge.execute(sid,Port())
        self.assertEqual(((275,),),result.rows)
        with self.assertRaises(Exception): bridge.execute(sid,Port())

    def test_filtered_question_cannot_enter_execution_bridge(self):
        from backend.semantics.recovery import SemanticRecovery
        bridge=SemanticRecovery(self.adapter())
        with self.assertRaisesRegex(ValueError,'filters_not_executable'):
            bridge.start(public_request(self.pack,'不要超过20万元的艺术家'),recorded_at='2026-10-23T10:00:00Z')
        self.assertEqual({},bridge._sessions)


class CrossDomainAdapterTests(unittest.TestCase):
    def test_actual_c_v101_packs_and_auto_resolution(self):
        root=Path(os.environ.get('BATCH1_C_ROOT', ROOT))
        if not (root/'domain-packs/retail_inventory/v1/manifest.json').is_file():
            self.skipTest('Requires merged C domain metadata or explicit isolated BATCH1_C_ROOT')
        validator=contract_module()
        registry=DomainPackRegistry(Path(root)/'domain-packs')
        for domain,question in (('retail_inventory','商品库存数量'),('equipment_loan','器材借用数量')):
            pack=registry.load_path(Path(root)/'domain-packs'/domain/'v1')
            self.assertEqual('1.0.1',pack.version)
            request=public_request(pack,question)
            result=PublicSemanticAdapter(pack,contract_validator=validator).analyze(request)
            validator.validate_contract_pair(request,result,pack)
            self.assertEqual('auto_domain_pack',result['required_slot_resolution']['source'])
            self.assertFalse(result['sql_execution_authorized'])
            from backend.semantics.recovery import SemanticRecovery
            with self.assertRaisesRegex(ValueError,'semantic_only_domain'):
                SemanticRecovery(PublicSemanticAdapter(pack,contract_validator=validator)).start(request,recorded_at='2026-10-23T10:00:00Z')

    def test_explicit_session_conflict_is_preserved_and_escalated(self):
        validator=contract_module()
        pack=DomainPackRegistry(ROOT/'domain-packs').load_path(ROOT/'domain-packs/chinook/v1')
        request=public_request(pack,'艺术家')
        request['session']['slot_observations']=[{'slot_id':'slot.entity_type','values':['album'],
                                                'evidence_refs':['evidence.entity_kinds'],'source':'explicit_session'}]
        result=PublicSemanticAdapter(pack,contract_validator=validator).analyze(request)
        observation=next(x for x in result['slot_observations'] if x['slot_id']=='slot.entity_type')
        self.assertEqual(['album'],observation['values'])
        self.assertIn('session_observation_conflict',[i['reason_code'] for i in result['issues']])
        self.assertEqual('escalate',result['typed_decision']['recommended_action'])

    def test_sufficient_query_no_user_question_and_wrong_table_zero_execution(self):
        from backend.semantics.recovery import SemanticRecovery
        from backend.agent.clarification_recovery import SqlProposal
        validator=contract_module()
        pack=DomainPackRegistry(ROOT/'domain-packs').load_path(ROOT/'domain-packs/chinook/v1')
        bridge=SemanticRecovery(PublicSemanticAdapter(pack,contract_validator=validator))
        request=public_request(pack,'统计专辑数量')
        result=bridge.start(request,recorded_at='2026-10-23T10:00:00Z')
        sid=request['session']['session_id']
        self.assertIsNone(bridge.pending(sid))
        class Wrong:
            def propose(self,**kwargs): return SqlProposal('proposal.wrong','SELECT COUNT(*) FROM Artist',('Artist',))
        with self.assertRaises(Exception): bridge.execute(sid,Wrong())
        self.assertEqual(0,bridge._sessions[sid].local_sqlite_execution_attempts)
        class Correct:
            def propose(self,**kwargs): return SqlProposal('proposal.album','SELECT COUNT(*) FROM Album',('Album',))
        self.assertEqual(((347,),),bridge.execute(sid,Correct()).rows)


class MergedEntrypointTests(unittest.TestCase):
    def test_default_validator_and_local_packs_without_external_roots(self):
        from unittest.mock import patch
        with patch.dict(os.environ):
            for name in ('BATCH1_A_ROOT', 'BATCH1_C_ROOT'):
                os.environ.pop(name, None)
            pack=DomainPackRegistry(ROOT/'domain-packs').load_path(ROOT/'domain-packs/chinook/v1')
            adapter=PublicSemanticAdapter(pack)
            result=adapter.analyze(public_request(pack,'😀艺术家不要超过20万元'))
            self.assertEqual('1.0.0',result['result_version'])
            self.assertEqual('😀艺术家不要超过20万元',result['rewritten_question'])
            self.assertFalse(result['sql_execution_authorized'])
            self.assertEqual(ROOT/'tools/evaluation/validate_batch1_semantic_contracts.py',
                             Path(adapter.validator.__file__).resolve())
            for domain in ('retail_inventory','equipment_loan'):
                local=DomainPackRegistry(ROOT/'domain-packs').load_path(ROOT/'domain-packs'/domain/'v1')
                self.assertEqual('1.0.1',local.version)
                output=PublicSemanticAdapter(local).analyze(public_request(local,'😀未知类型'))
                self.assertEqual('auto_domain_pack',output['required_slot_resolution']['source'])
                self.assertFalse(output['sql_execution_authorized'])
