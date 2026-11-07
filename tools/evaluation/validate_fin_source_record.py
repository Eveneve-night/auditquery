"""Offline test bridge to explicitly selected A production validator, no HTTP."""
import argparse,importlib.util,json,sys
from pathlib import Path
def main():
    p=argparse.ArgumentParser();p.add_argument('--contract-root',type=Path,required=True);a=p.parse_args()
    spec=importlib.util.spec_from_file_location('fin_contract',a.contract_root/'src/backend/data_sources/contracts.py')
    validator=importlib.util.module_from_spec(spec);spec.loader.exec_module(validator)
    raw=json.load(sys.stdin)
    try:
        validator.validate_source(raw['record'])
        previous=raw.get('context',{}).get('previous')
        if previous and raw['record']['status']!=previous['status']:
            validator.validate_transition(previous,raw['record']['status'],expected_revision=previous['revision'],actor_role='admin',operation_id='offline.component.check')
    except validator.DataSourceContractError:
        print(json.dumps({'valid':False,'code':'invalid_contract'}));return
    print(json.dumps({'valid':True}))
if __name__=='__main__':main()
