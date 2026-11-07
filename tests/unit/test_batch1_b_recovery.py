"""Offline recovery boundary probes; mock SQL is not model accuracy evidence."""
import json
import unittest
from tests.unit.test_local_repairs import FakeGateway, request, service
from tests.unit.test_d4_online_sql_service import answer_request


class Batch1RecoveryBoundaryTests(unittest.TestCase):
    def test_sufficient_query_uses_original_gate_without_user_question(self):
        gateway = FakeGateway()
        result = service(gateway).submit(request('统计专辑数量'))
        self.assertEqual('success', result['status'])
        self.assertIsNone(result['pending_question'])
        self.assertEqual('approved', result['sql']['safety_status'])
        self.assertEqual(1, result['sql']['execution_attempts'])

    def test_automatic_missing_metric_then_entity_merge_and_replay_guard(self):
        gateway = FakeGateway()
        api = service(gateway)
        result = api.submit(request('艺术家'))
        self.assertEqual('clarification_required', result['status'])
        self.assertEqual('slot.metric', result['pending_question']['slot_id'])
        self.assertEqual(0, len(gateway.requests))
        answer = answer_request(result, 'count', 'answer.batch.metric')
        result = api.submit(answer)
        # Existing D4 asks the entity confirmation separately. Do not call this
        # a one-question end-to-end completion until automatic binding exists.
        if result['pending_question'] is not None:
            answer = answer_request(result, 'artist', 'answer.batch.entity')
            result = api.submit(answer)
        self.assertEqual('success', result['status'])
        self.assertEqual('approved', result['sql']['safety_status'])
        with self.assertRaisesRegex(Exception, 'replayed'):
            api.submit(answer)

    def test_negation_units_remain_in_original_provider_input(self):
        gateway = FakeGateway()
        api = service(gateway)
        query = '统计艺术家数量，不包括超过20万元的项目'
        result = api.submit(request(query))
        self.assertEqual('clarification_required', result['status'])
        result = api.submit(answer_request(result, 'count', 'answer.batch.filter1'))
        if result['pending_question'] is not None:
            result = api.submit(answer_request(result, 'artist', 'answer.batch.filter2'))
        payload = json.loads(gateway.requests[-1].messages[-1].content)
        self.assertEqual(query, payload['query'])
        # This checks preservation only. The mock query does not implement the
        # business filter and cannot prove semantic execution correctness.

    def test_write_proposal_never_reaches_executor(self):
        result = service(FakeGateway('write')).submit(request('统计专辑数量'))
        self.assertNotEqual('success', result['status'])
        if result['sql'] is not None:
            self.assertEqual(0, result['sql']['execution_attempts'])

class SemanticObservationTests(unittest.TestCase):
    def test_entity_is_metadata_derived_metric_stays_missing(self):
        from src.backend.agent.domain_pack import DomainPackRegistry
        from src.backend.semantics.observations import enum_observations
        pack = DomainPackRegistry().load('chinook_music_store', '1.0.0')
        observations = enum_observations('艺术家', pack)
        self.assertEqual(('artist',), observations['slot.entity_type'].values)
        self.assertNotIn('slot.metric', observations)
        for query in ('不要统计艺术家超过20万元的收入', '非艺术家', '没有艺术家', '未归类艺术家'):
            self.assertEqual({}, enum_observations(query, pack))

    def test_one_missing_metric_question_then_original_safety_gate(self):
        from src.backend.agent.domain_pack import DomainPackRegistry
        from src.backend.agent.clarification_recovery import D4ClarificationRecoveryRuntime, build_clarification_answer
        from src.backend.agent.intent_routing import create_conversation_state, apply_intent_result
        from src.backend.api.online_sql_service import _sql_intent_result
        from src.backend.agent.intent_routing import load_source_catalog
        from src.backend.semantics.observations import enum_observations
        from src.backend.agent.clarification_recovery import SqlProposal
        class FakeProposalPort:
            def propose(self, **kwargs):
                return SqlProposal('proposal.batch', 'SELECT COUNT(*) FROM Artist', ('Artist',))
        from pathlib import Path
        pack = DomainPackRegistry().load('chinook_music_store', '1.0.0')
        runtime = D4ClarificationRecoveryRuntime(pack)
        root = Path(__file__).resolve().parents[2]
        catalog = load_source_catalog(root / 'configs/routing/d4-01/source-catalog.v1.json')
        state = apply_intent_result(create_conversation_state('session.batch.semantic', '艺术家'), catalog, _sql_intent_result())
        session = runtime.start(conversation_state=state, slot_observations=enum_observations('艺术家', pack),
                                request_id='request.batch.semantic', recorded_at='2026-10-23T09:00:00Z')
        pending = session.pending_question.to_dict()
        self.assertEqual('slot.metric', pending['slot_id'])
        option = next(x for x in pending['options'] if x['value'] == 'count')
        answer = {k: pending[k] for k in ('question_id','session_id','request_id','turn_id','state_revision','slot_id')}
        answer.update(answer_id='answer.batch.semantic', values=['count'], evidence_refs=[option['evidence_ref']])
        runtime.resume(session, build_clarification_answer(answer), recorded_at='2026-10-23T09:01:00Z')
        self.assertIsNone(session.pending_question)
        result = runtime.handoff_and_execute(session, proposal_port=FakeProposalPort())
        self.assertIsNotNone(result)
        self.assertTrue(session.safety_approved)
        self.assertEqual(1, session.safety_gate_calls)
