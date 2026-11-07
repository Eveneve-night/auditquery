"""Public-development replay only; consumes query strings, never answer rows.

Run: python -m src.backend.semantics.replay --pack domain-packs/chinook/v1 --query 唱片
This is an internal diagnostic report, not a new public API or frozen metric.
"""
import argparse
import hashlib
import json
from pathlib import Path
from .conservative import ConservativeSemantics, VERSION
from ..agent.domain_pack import DomainPackRegistry


def replay(pack_path: Path, queries: list[str], *, pack_root: Path | None = None) -> dict:
    if not isinstance(queries, list) or not queries or not all(isinstance(x, str) for x in queries):
        raise ValueError('only query strings accepted')
    registry = DomainPackRegistry(pack_root) if pack_root is not None else DomainPackRegistry()
    pack = registry.load_path(pack_path)
    engine = ConservativeSemantics(pack)
    results = [engine.analyze(query) for query in queries]
    digest = hashlib.sha256(json.dumps(queries, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return {'implementation_version': VERSION, 'split': 'prompt_dev',
            'evidence_kind': 'public_development_replay', 'input_sha256': digest,
            'domain_pack_sha256': pack.manifest_sha256,
            'path': 'rules', 'model_calls': 0, 'credential_reads': 0, 'cost_cny': 0,
            'cases_run': len(results), 'results': results,
            'not_proven': ['business_accuracy', 'hidden_generalization', 'five_group_comparison', 'zero_code_migration']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pack', required=True, type=Path)
    parser.add_argument('--query', action='append', required=True)
    parser.add_argument('--pack-root', type=Path, help='Explicit trusted local pack root; no automatic escape')
    args = parser.parse_args()
    print(json.dumps(replay(args.pack, args.query, pack_root=args.pack_root), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
