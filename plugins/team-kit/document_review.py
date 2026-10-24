"""Confined document reports; dependencies run only in the existing document venv."""
import importlib.util
import copy
import json
from pathlib import Path
import subprocess


def schema(name, description, properties, required):
    return {'name': name, 'description': description, 'parameters': {
        'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}


SOURCE = {'type': 'object', 'properties': {'path': {'type': 'string'}, 'sheet': {'type': 'string'},
          'header_row': {'type': 'integer', 'minimum': 1, 'maximum': 100}}, 'required': ['path'], 'additionalProperties': False}
STRINGS = {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 10}
SCHEMAS = {
    'table_analyze': schema('table_analyze', 'Read-only Excel/CSV comparison by explicit key columns, or exact numeric aggregation. Returns source row/cell references, duplicate keys and missing values. Never evaluates formulas.', {
        'action': {'type': 'string', 'enum': ['compare', 'aggregate']},
        'sources': {'type': 'array', 'items': SOURCE, 'minItems': 1, 'maxItems': 10},
        'keys': STRINGS, 'columns': STRINGS, 'group_by': STRINGS}, ['action', 'sources']),
    'doc_compare': schema('doc_compare', 'Read-only body-text diff of two TXT/Markdown, Word, HWP/HWPX or text PDFs. Returns paragraph/page/line references. Does not compare layout, images or OCR scanned pages.', {
        'before': {'type': 'string'}, 'after': {'type': 'string'}}, ['before', 'after']),
}


def handle(name, args, *, files_config, **_):
    args = copy.deepcopy(args)
    spec = importlib.util.spec_from_file_location('review_files', Path(__file__).with_name('files.py'))
    files = importlib.util.module_from_spec(spec); spec.loader.exec_module(files)
    if not isinstance(files_config, dict) or files_config.get('enabled') is not True:
        raise ValueError('files_disabled')
    roots = files.folders(files_config)
    if not roots:
        raise ValueError('configured_document_folders_unavailable')
    paths = [s.get('path') for s in args.get('sources', []) if isinstance(s, dict)] if name == 'table_analyze' else [args.get('before'), args.get('after')]
    if name == 'table_analyze' and (not isinstance(args.get('sources'), list) or len(paths) != len(args['sources'])):
        raise ValueError('invalid_document_sources')
    if not paths or len(paths) > 10:
        raise ValueError('invalid_document_sources')
    stamps = {}
    for index, raw in enumerate(paths):
        path = files.inside(raw, [*roots, files.attachments(), files.image_attachments()])
        if path is None or not path.is_file() or path.stat().st_size > 25 * 1024 * 1024:
            raise ValueError('document_outside_scope_missing_or_larger_than_25MB')
        suffixes = {'.xlsx', '.csv'} if name == 'table_analyze' else {'.txt', '.md', '.docx', '.pdf', '.hwp', '.hwpx'}
        if path.suffix.lower() not in suffixes:
            raise ValueError('unsupported_review_document_type')
        stamps[path] = (path.stat().st_mtime_ns, path.stat().st_size)
        if name == 'table_analyze': args['sources'][index]['path'] = str(path)
        else: args['before' if index == 0 else 'after'] = str(path)
        for root in roots:
            if root == path or root in path.parents:
                if reason := files._access(root):
                    raise ValueError(reason)
    if not files.DOC_PYTHON.is_file():
        raise ValueError('document_environment_missing')
    result = subprocess.run([str(files.DOC_PYTHON), str(Path(__file__).with_name('document_review_worker.py'))],
        input=json.dumps({'name': name, 'args': args}), capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ValueError('document_review_worker_failed')
    if any((p.stat().st_mtime_ns, p.stat().st_size) != stamp for p, stamp in stamps.items()):
        raise ValueError('source_changed_during_comparison_read_again')
    return json.loads(result.stdout)
