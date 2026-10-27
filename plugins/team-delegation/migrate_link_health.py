"""Enrich/retire local source links. Default preview uses an isolated SQLite copy.

Rollback: stop the watcher, restore the pre-apply SQLite backup with sqlite3
backup API, then restore the previous plugin version. Native originals and work
statuses are never deleted or completed by this migration.
"""
from pathlib import Path
import argparse
import fcntl
import json
import sqlite3
import tempfile
import time
from collections import Counter


def backup(source, target):
    with sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True) as src, sqlite3.connect(target) as dest:
        src.backup(dest)
    Path(target).chmod(0o600)


def analyze_work(item, sources):
    """Read-only work-level diagnosis, using the reminder semantic comparison."""
    import work_content as content
    from source_lookup import SourceReadError
    refs=sources.references(item)
    if any(r.get('metadata',{}).get('link_state',{}).get('status')=='transient'
           for r in item['sources']+item.get('knowledge_refs',[])):
        return {'status':'transient','reason':'source_read_pending'}
    if not refs:
        return {'status':'excluded','reason':'no_sync_originals'}
    try:
        snapshots=[sources.read(kind,sid)['content'] for kind,sid in refs if kind in {'notes','notion'}]
        if any(task.get('ambiguous') for snapshot in snapshots for task in snapshot['tasks']):
            return {'status':'conflict','reason':'ambiguous_task_deadline'}
        tasks=[content.digest(content.semantic(snapshot)['tasks']) for snapshot in snapshots]
        if len(set(tasks))>1:
            return {'status':'conflict','reason':'linked_task_sources_conflict'}
        if len({kind for kind,_ in refs})<len(refs):
            return {'status':'unsupported','reason':'single_original_per_domain_required'}
        return {'status':'ok','reason':'read_ok'}
    except SourceReadError as exc:
        return {'status':exc.status,'reason':exc.reason}
    except Exception:
        return {'status':'transient','reason':'work_analysis_read_failed'}


def migrate(database, home, *, apply=False, backup_dir=None, reader=None, sources=None):
    from common_work_links import CommonWorkStore
    from source_identity import Reader
    from link_lifecycle import Lifecycle
    database,home=Path(database).resolve(),Path(home).resolve()
    backup_path=None
    with tempfile.TemporaryDirectory(prefix='link-health-preview-') as temp:
        if apply:
            if backup_dir is None:
                raise ValueError('apply requires backup_dir outside the repository')
            folder=Path(backup_dir).resolve();folder.mkdir(parents=True,exist_ok=True)
            backup_path=folder/('common-work-before-link-health-'+str(time.time_ns())+'.sqlite3')
            backup(database,backup_path)
            target=database
        else:
            target=Path(temp)/'preview.sqlite3';backup(database,target)
        store=CommonWorkStore(target)
        engine=Lifecycle(store,reader or Reader(home))
        from work_sources import Sources
        sources=sources or Sources(home)
        rows=[]
        works=[]
        with store._connect() as conn:
            ids=[r[0] for r in conn.execute("SELECT common_id FROM common_work WHERE status='active'")]
        for cid in ids:
            item=store.get(cid)
            for decision in engine.inspect(item):
                rows.append({'common_id':cid,'title':item['title'],**decision})
            works.append({'common_id':cid,'title':item['title'],**analyze_work(store.get(cid),sources)})
        return {'apply':apply,'backup':str(backup_path) if backup_path else None,
                'changed':sum(bool(r.get('changed')) for r in rows),
                'counts':dict(Counter(r['status'] for r in rows)), 'links':rows,
                'work_counts':dict(Counter(r['status'] for r in works)), 'works':works}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,default=Path.home()/'.hermes/shared/common-work-links.sqlite3')
    parser.add_argument('--home',type=Path,default=Path.home()/'.hermes/profiles/work-assistant')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--apply',action='store_true')
    mode.add_argument('--dry-run',action='store_true')
    parser.add_argument('--backup-dir',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    lock=args.home/'state/notion-completion-sync.lock'
    with lock.open('a') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX)
        result=migrate(args.database,args.home,apply=args.apply,backup_dir=args.backup_dir)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2))
        args.output.chmod(0o600)
    print(json.dumps({k:v for k,v in result.items() if k not in {'links','works'}},ensure_ascii=False))


if __name__=='__main__':main()
