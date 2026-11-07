"""Public-development replay: runtime inputs and scoring labels are separate."""
import argparse
import json
from pathlib import Path
from ..agent.domain_pack import DomainPackRegistry
from .linker import SchemaLinker


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--pack',type=Path,default=Path('domain-packs/order_to_cash/v1'))
    parser.add_argument('--question',required=True)
    parser.add_argument('--mode',choices=['lexical','enhanced'],default='enhanced')
    args=parser.parse_args()
    pack=DomainPackRegistry().load_path(args.pack)
    engine=SchemaLinker(pack)
    print(json.dumps(engine.locate({'question':args.question,'authorized_ids':engine.public_catalog_ids(),'mode':args.mode}),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
