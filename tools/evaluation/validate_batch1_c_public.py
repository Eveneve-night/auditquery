"""Recomputable public development evidence; never a blind/business evaluation.

--implementation-root selects B's real checkout, not a simulated backend.
Only case.input fields reach analyze(); coverage metadata is never passed in.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / 'tests/fixtures/batch1-public/v1'
DOMAINS = ('retail_inventory', 'equipment_loan')
BASELINE_MAIN = '1eb63d0d495ef44b2fdb779340b2601b3f114e54'
DEFAULT_REPORT = ROOT / 'tests/reports/batch1-c-public-v2.json'
RECOVERY_REPORT = 'tests/reports/batch1-c-public-recovery-v1.json'

def canonical_utf8_lf(path):
    """Strict UTF-8 content; preserve all text except line ending encoding."""
    return path.read_bytes().decode('utf-8').replace('\r\n', '\n').replace('\r', '\n').encode('utf-8')

def sha(path):
    return hashlib.sha256(canonical_utf8_lf(path)).hexdigest()

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
                                    separators=(',',':')).encode('utf-8')).hexdigest()

def configuration_metrics(pack):
    """Measure only manifest.json plus its eight validated declared files.

    The production Registry validates the exact file set before this helper is
    called. Do not use directory iteration or worktree physical sizes here.
    """
    names = sorted({'manifest.json', *(entry['path'] for entry in pack.manifest['files'])})
    records=[]
    for name in names:
        content=canonical_utf8_lf(pack.pack_path/name)
        records.append({'path':name,'canonical_utf8_lf_bytes':len(content),
                        'sha256':hashlib.sha256(content).hexdigest()})
    return {'configuration_canonical_utf8_lf_bytes':sum(r['canonical_utf8_lf_bytes'] for r in records),
            'configuration_file_count':len(records),'configuration_files':records,
            'configuration_content_sha256':digest(records)}

def runtime_input(case):
    """An explicit allowlist keeps scoring fields out of runtime inputs."""
    value = case['input']
    return {'domain_pack_id': value['domain_pack_id'], 'question': value['question']}

def build_report(implementation_root=None):
    sys.path.insert(0, str(ROOT / 'src'))
    from backend.agent.domain_pack import DomainPackRegistry
    registry = DomainPackRegistry(ROOT / 'domain-packs')
    manifest = json.loads((ASSETS / 'manifest.json').read_text(encoding='utf-8'))
    assert sha(ASSETS / 'inputs.json') == manifest['input_sha256']
    cases = json.loads((ASSETS / 'inputs.json').read_text(encoding='utf-8'))
    assert len(cases) == manifest['case_count']
    assert all(c['split'] == 'prompt_dev' for c in cases)
    assert sha(ASSETS / 'scoring.json') == manifest['scoring_sha256']
    scoring = {row['case_id']: row for row in json.loads(
        (ASSETS / 'scoring.json').read_text(encoding='utf-8'))}
    packs = {d: registry.load(d, '1.0.1' if d in DOMAINS else '1.0.0')
             for d in (*DOMAINS, 'chinook_music_store')}
    metadata = {d: {'manifest_sha256': p.manifest_sha256, 'version':p.version,
                   **configuration_metrics(p),
                   'configuration_items': {role: len(p.artifact_items(role)) for role in
                       ('concepts', 'aliases', 'slots', 'schema_catalog', 'relations', 'value_evidence', 'validation_rules')}}
                for d,p in packs.items()}
    rows = []
    implementation = None
    if implementation_root:
        import backend
        path = Path(implementation_root).resolve() / 'src/backend'
        backend.__path__.insert(0, str(path))
        from backend.semantics.conservative import ConservativeSemantics
        module = path / 'semantics/conservative.py'
        implementation = {'path': 'src/backend/semantics/conservative.py', 'sha256': sha(module)}
        implementation['source_files'] = {f.name:sha(f) for f in sorted(module.parent.glob('*.py'))}
        # A stable content binding is portable across LF/CRLF checkouts and
        # report-only commits. Git HEAD/dirty state are environmental metadata.
        implementation['source_manifest_sha256'] = digest(implementation['source_files'])
        for case in cases:
            args = runtime_input(case)
            result = ConservativeSemantics(packs[args['domain_pack_id']]).analyze(args['question'])
            # Check source spans and exact edit reconstruction, not semantic F1.
            query = args['question']
            spans_ok = all(query[m['start']:m['end']] == m['text'] for m in result['mentions'])
            rebuilt = query
            for edit in reversed(result['changes']):
                assert query[edit['start']:edit['end']] == edit['before']
                rebuilt = rebuilt[:edit['start']] + edit['after'] + rebuilt[edit['end']:]
            preserve = rebuilt == result['rewritten_query']
            # These tokens must survive alias normalization verbatim. This is
            # a narrow safety regression, not a semantic equivalence metric.
            protected = ('不要', '12', '120', '2025', '2026', '重新开始')
            protected_ok = all(result['rewritten_query'].count(t) == query.count(t)
                               for t in protected if t in query)
            ambiguity_ok = not any(
                m['status'] == 'ambiguous' and any(
                    e['start'] < m['end'] and m['start'] < e['end']
                    for e in result['changes']) for m in result['mentions'])
            no_execution = result['sql_execution_authorized'] is False and result['model_calls'] == 0
            # Public scoring is consumed only after the real implementation
            # returns. It is never sent to the engine or the domain registry.
            score = scoring.get(case['case_id'])
            rewrite_match = result['rewritten_query'] == score['expected_rewrite'] if score else None
            clarify_match = (result['requires_clarification'] == score['expected_clarification']
                             if score and score['expected_clarification'] is not None else None)
            rows.append({'case_id':case['case_id'], 'family':case['template_family'],
                         'domain':args['domain_pack_id'], 'spans_valid':spans_ok,
                         'edits_reconstruct':preserve, 'no_execution':no_execution,
                         'protected_tokens_preserved':protected_ok,
                         'ambiguous_span_not_rewritten':ambiguity_ok,
                         'public_rewrite_exact_match':rewrite_match,
                         'public_ambiguity_check':clarify_match,
                         'mentions':len(result['mentions']),
                         'clarification':result['requires_clarification'],
                         'passed':spans_ok and preserve and no_execution and protected_ok and ambiguity_ok
                                  and rewrite_match is not False and clarify_match is not False})
    recovery=json.loads((ROOT/RECOVERY_REPORT).read_text(encoding='utf-8'))
    source={'tools/evaluation/validate_batch1_c_public.py':sha(Path(__file__))}
    result = {'report_version':'2.0.0','split':'prompt_dev','case_count':len(cases),
            'measurement':{'version':'configuration-canonical-utf8-lf.v1',
                           'file_set':'manifest.json plus manifest.files paths, sorted',
                           'physical_disk_sizes_included':False},
            'source_binding':{'baseline_main':BASELINE_MAIN,'generator_files':source,
                              'generator_manifest_sha256':digest(source)},
            'asset_sha256':sha(ASSETS/'inputs.json'), 'domain_packs':metadata,
            'int01':{'scope':'public_component_invariants_only','implementation':implementation,
                     'executed':len(rows),'passed':sum(r['passed'] for r in rows),'rows':rows,
                     'public_rewrite_scored':sum(r['public_rewrite_exact_match'] is not None for r in rows),
                     'public_rewrite_matched':sum(r['public_rewrite_exact_match'] is True for r in rows),
                     'semantic_f1':None,'rewrite_business_accuracy':None},
            'int02':{'registry_loaded':len(packs),'code_or_prompt_changes_for_registry':0,
                     'freeze_before_onboarding':False,'person_hours':None,
                     'migration_target_proven':False},
            'task3':{'executed':0,'status':'not_executed_by_this_component_report',
                     'in_process_public_contract':'already_merged',
                     'recovery_evidence':{'path':RECOVERY_REPORT,'sha256':sha(ROOT/RECOVERY_REPORT),
                                          'reported_checks':recovery['passed'],
                                          'scope':'historical_actual_public_in_process_recovery'},
                     'HTTP':'not_implemented'},
            'boundaries':{'model_calls':0,'key_reads':0,'cost_cny':0,
                          'validation_exposures':0,'hidden_test_created':False},
            'not_proven':['business_accuracy','unseen_generalization','innovation',
                          'frozen_code_zero_change_migration','new_domain_HTTP_recovery']}
    result['stable_sha256'] = digest(result)
    return result

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--implementation-root',type=Path,default=ROOT)
    parser.add_argument('--report',type=Path)
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    report=build_report(args.implementation_root)
    encoded=json.dumps(report,ensure_ascii=False,indent=2)+'\n'
    if args.check:
        target=args.report or DEFAULT_REPORT
        committed=json.loads(target.read_text(encoding='utf-8'))
        if committed != report:
            print(json.dumps({'status':'fail','error':'committed_report_drifted'}))
            return 1
        print(json.dumps({'status':'pass','report_version':report['report_version']}))
    elif args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(encoded,encoding='utf-8',newline='\n')
    else:
        print(encoded)
    return 1 if any(not r['passed'] for r in report['int01']['rows']) else 0

if __name__=='__main__':
    raise SystemExit(main())
