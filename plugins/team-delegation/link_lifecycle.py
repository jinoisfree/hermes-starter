"""Local link health. Never search by work title or recreate/delete a native object."""
from source_lookup import SourceReadError
import traceback
from pathlib import Path

TERMINAL = {'missing','out_of_scope','unsupported','conflict'}


def references(item):
    return [(r['domain'],r['source_id'],r.get('metadata',{})) for r in item['sources']] + [
        ('notion',r['external_id'],r.get('metadata',{})) for r in item.get('knowledge_refs',[])
        if r['ref_type']=='notion_page']


class Lifecycle:
    def __init__(self, store, reader):
        self.store,self.reader=store,reader

    def inspect(self, item, *, dry_run=False):
        """Return decisions and transitions. Missing manual replacements need owner choice.

        Agent-driven note rebuilds bypass searching and use replace_note_source.
        Terminal outcomes are stable until an explicit relink changes the source.
        """
        old={(r['domain'],r['source_id']):r for r in self.store.source_health(item['common_id'])}
        decisions=[]
        for kind,sid,metadata in references(item):
            previous=old.get((kind,sid))
            if previous and previous['status'] in TERMINAL:
                decisions.append({'domain':kind,'source_id':sid,'status':previous['status'],
                                  'reason':previous['reason'],'changed':False})
                continue
            trusted=None
            failure=None
            if metadata.get('owner_scope')=='personal' or (kind=='calendar' and sid.startswith('google:')):
                status,reason='out_of_scope','personal_source'
            else:
                try:
                    trusted=self.reader(kind,sid)
                    if trusted['owner_scope']=='personal':
                        status,reason='out_of_scope','personal_source'
                    elif kind=='calendar' and trusted['identity'].get('recurring'):
                        status,reason='unsupported','recurring_calendar'
                    else:
                        status,reason='ok','read_ok'
                except SourceReadError as exc:
                    status,reason=exc.status,exc.reason
                    failure=exc
                except Exception as exc:
                    status,reason='transient','source_read_unclassified'
                    failure=exc
            if kind=='calendar' and status=='missing' and metadata.get('identity'):
                from source_identity import validated
                try:
                    saved=validated('calendar',metadata)
                    search=getattr(self.reader,'calendar_candidates',None)
                    if search is None:
                        raise SourceReadError('transient','calendar_candidate_reader_unavailable')
                    candidates=[(new_id,value) for new_id,value in search(metadata)
                                if new_id!=sid and validated('calendar',value)==saved]
                    if len(candidates)==1 and self.store.find_by_source(domain='calendar',source_id=candidates[0][0]) is None:
                        new_id,trusted=candidates[0]
                        if dry_run or self.store.relink_calendar(item['common_id'],sid,new_id,trusted):
                            decisions.append({'domain':kind,'source_id':new_id,'old_id':sid,
                                              'status':'ok','reason':'relinked','changed':True,'identity':trusted})
                            continue
                except SourceReadError as exc:
                    status,reason,failure='transient',exc.reason,exc
                except Exception as exc:
                    status,reason,failure='transient','calendar_candidate_read_failed',exc
            changed=not previous or (previous['status'],previous['reason']) != (status,reason)
            if not dry_run:
                event=self.store.record_source_state(item['common_id'],kind,sid,status,reason,identity=trusted)
                changed=bool(event)
            decisions.append({'domain':kind,'source_id':sid,'status':status,'reason':reason,
                              'changed':changed,'identity':trusted,
                              'report':changed and status not in {'ok','out_of_scope'},
                              'needs_owner_link':status=='missing',
                              'exception_type':type(failure).__name__ if failure else None,
                              'traceback':[{'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name}
                                           for frame in traceback.extract_tb(failure.__traceback__)] if failure else []})
        return decisions
