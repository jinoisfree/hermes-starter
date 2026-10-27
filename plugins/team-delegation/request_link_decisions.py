"""Preview missing-source decisions; sending requires an explicitly enabled feature."""
from pathlib import Path
import argparse
import json
import tempfile
from common_work_links import CommonWorkStore
from link_decisions import Decisions, Inventory
from migrate_link_health import backup


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--home',type=Path,default=Path.home()/'.hermes/profiles/work-assistant')
    p.add_argument('--database',type=Path,default=Path.home()/'.hermes/shared/common-work-links.sqlite3')
    mode=p.add_mutually_exclusive_group()
    mode.add_argument('--dry-run',action='store_true')
    mode.add_argument('--request-decisions',action='store_true')
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    with tempfile.TemporaryDirectory(prefix='link-decision-preview-') as folder:
        database=args.database
        if not args.request_decisions:
            database=Path(folder)/'preview.sqlite3';backup(args.database,database)
        result=Decisions(args.home,CommonWorkStore(database),Inventory(args.home)).request(dry_run=not args.request_decisions)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2));args.output.chmod(0o600)
    from collections import Counter
    print(json.dumps({'sent':result['sent'],'statuses':dict(Counter(r['status'] for r in result['rows'])),
                      'certain':sum(r.get('certain',False) for r in result['rows'])},ensure_ascii=False))


if __name__=='__main__':main()
