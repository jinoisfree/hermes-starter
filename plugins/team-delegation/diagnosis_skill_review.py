"""Read-only mechanical skill quality checks for system-doctor diagnosis.

Ported from moonlight-lupin/agent-skills `software-development/skill-quality-review`
(scripts/check_references.py and the mechanical checks in references/batch-checks.md)
https://github.com/moonlight-lupin/agent-skills/tree/ae671a22253ed3dfe81a96d8405aecd3fc9baec6/software-development/skill-quality-review
commit ae671a22253ed3dfe81a96d8405aecd3fc9baec6

MIT License, Copyright (c) 2025-2026 moonlight-lupin. Permission is hereby granted,
free of charge, to any person obtaining a copy of this software and associated
documentation files, to deal in the Software without restriction, subject to the
condition that the above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software. THE SOFTWARE IS
PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.

Ported checks (upstream numbers): 1 frontmatter, 4 completion criteria, 6 name match,
7 boilerplate duplication, 9 no-op prose, 10 trigger wording, 11 metadata.hermes,
12 literal duplicate lines, 15 cited files exist (existence only; nothing is executed),
17 reference hygiene (TOC, depth, orphans), 18 progress checklist; plus Hermes library
size targets, duplicated descriptions, unresolved related_skills and machine-local paths.
Judgement checks (2, 3, 8, 13, 14, 16, 19) are not mechanical and are left to the reviewer.
Korean-library adaptation: a trigger may be "Use when" or a Korean condition
(때/경우/요청/물으면/하면), and is MINOR rather than MAJOR. Never writes or executes skills.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import json
import re

MAX_SKILLS = 400
MAX_FILES_PER_SKILL = 60
MAX_TEXT = 1_000_000
SEVERITY = {'MAJOR': 0, 'MINOR': 1, 'NIT': 2}
FRONTMATTER_RE = re.compile(r'^---\n(.*?)\n---\n?', re.S)
TOC_RE = re.compile(r'^(#{1,3}\s+(contents|table of contents|목차)\b|## Contents)', re.I | re.M)
TRIGGER_RE = re.compile(r'use when|때|경우|요청|물으면|하면|원하면', re.I)
NO_OPS = ('be careful', 'be thorough', 'best practices', 'use caution', 'be mindful', 'make sure', 'ensure that',
          'it is important', 'note that', '신중하게', '꼼꼼하게', '주의 깊게')
CITED_RE = re.compile(r'(?<![\w/.-])((?:references|scripts|templates|assets)/[A-Za-z0-9_.\-/가-힣]+\.[A-Za-z0-9]{1,6})(?![\w/-])')
LOCAL_PATH_RE = re.compile(r'(?<![\w~])(/Users/[^/\s`\'"]+/|/home/[^/\s`\'"]+/|/root/)')
ARCHIVE_PARTS = {'.archive', '.curator_backups', '__pycache__', '.hub'}
EXEMPT_PARTS = {'brands', 'vendor', 'examples'}
DOC_DIRS = ('references', 'templates', 'scripts', 'assets', 'commands', 'agents', 'hooks', 'tests')


def bundled_names(diag, root):
    path = root / '.bundled_manifest'
    if not path.is_file():
        return set()
    return {line.split(':', 1)[0].strip() for line in diag.read(path, [root]).splitlines() if line.strip()}


def usage(diag, root):
    path = root / '.usage.json'
    if not path.is_file():
        return {}
    try:
        data = json.loads(diag.read(path, [root]))
    except (ValueError, OSError):
        return {}
    return {k: {'use_count': v.get('use_count', 0), 'view_count': v.get('view_count', 0),
                'last_used_at': v.get('last_used_at'), 'state': v.get('state')}
            for k, v in data.items() if isinstance(v, dict)}


def discover(diag, root):
    skills, skipped = [], 0
    for path in sorted(root.rglob('SKILL.md')):
        relative = path.relative_to(root)
        if any(part.startswith('.') or part in ARCHIVE_PARTS for part in relative.parts):
            continue
        try:
            diag.safe(path, [root])
        except ValueError:
            skipped += 1
            continue
        skills.append(path)
        if len(skills) >= MAX_SKILLS:
            break
    return skills, skipped


def parse(diag, path, root):
    import hermes_yaml as yaml
    text = diag.read(path, [root])[:MAX_TEXT]
    match = FRONTMATTER_RE.match(text)
    if not match:
        return text, None, text, 'missing'
    try:
        meta = yaml.safe_load(match[1])
    except Exception:
        return text, None, text[match.end():], 'invalid_yaml'
    if not isinstance(meta, dict):
        return text, None, text[match.end():], 'not_a_mapping'
    return text, meta, text[match.end():], None


def sections(body):
    found = {}
    for match in re.finditer(r'^## (.+)\n(.*?)(?=^## |\Z)', body, re.M | re.S):
        found[match[1].strip()] = re.sub(r'\s+', ' ', match[2]).strip()
    return found


def review_one(diag, path, root, add, siblings=()):
    folder = path.parent
    text, meta, body, problem = parse(diag, path, root)
    name = folder.name
    if problem:
        add('MAJOR', 1, 'frontmatter_' + problem, 'SKILL.md frontmatter is ' + problem.replace('_', ' '))
        meta = {}
    declared = meta.get('name')
    description = meta.get('description')
    if not declared:
        add('MAJOR', 1, 'missing_name', 'frontmatter name is missing')
    elif str(declared).strip() != name:
        add('MINOR', 6, 'name_mismatch', 'frontmatter name differs from folder name')
    if not description or not str(description).strip():
        add('MAJOR', 1, 'missing_description', 'frontmatter description is missing')
        description = ''
    description = re.sub(r'\s+', ' ', str(description)).strip()
    if len(description) > 1024:
        add('MAJOR', 1, 'description_over_limit', 'description is %d chars (limit 1024)' % len(description))
    if description and not TRIGGER_RE.search(description) and 'disable-model-invocation' not in (text[:2000]):
        add('MINOR', 10, 'no_trigger_wording', 'description has no "Use when" or Korean trigger condition')
    if meta and not isinstance(meta.get('metadata'), dict):
        add('NIT', 11, 'missing_metadata', 'metadata block is missing')
    elif meta and not isinstance(meta['metadata'].get('hermes'), dict):
        add('MINOR', 11, 'missing_metadata_hermes', 'metadata.hermes nesting is missing')
    for field in ('version', 'author'):
        if meta and field not in meta:
            add('NIT', 1, 'missing_' + field, field + ' is missing in frontmatter')
    chars = len(text)
    if chars > 20000:
        add('MINOR', 'size', 'skill_md_over_20k', 'SKILL.md is %d chars; split detail into references/' % chars)
    lines = [l.strip() for l in body.splitlines()]
    dupes = [l for l, n in Counter(l for l in lines if len(l) >= 20 and not set(l) <= set('|-: `')).items() if n > 1]
    if dupes:
        add('MINOR', 12, 'duplicate_lines', '%d literal duplicate line(s)' % len(dupes))
    lower = body.lower()
    found = [p for p in NO_OPS if p in lower]
    if found:
        add('NIT', 9, 'no_op_prose', 'no-op phrases: ' + ', '.join(found[:4]))
    steps = re.findall(r'^###\s*\d+', body, re.M)
    if steps and not re.search(r'done when|완료 기준|완료 조건|\*\*done:?\*\*', lower):
        add('MINOR', 4, 'no_completion_criteria', '%d numbered step headings without completion criteria' % len(steps))
    numbered = re.findall(r'^\s*\d+\.\s', body, re.M)
    if len(numbered) >= 7 and '- [ ]' not in body:
        add('MINOR', 18, 'no_progress_checklist', '%d numbered steps without a "- [ ]" checklist' % len(numbered))
    if LOCAL_PATH_RE.search(text):
        add('NIT', 'portability', 'machine_local_path', 'absolute home path; prefer ~/ or a profile-relative path')
    # Check 15 (existence only) and 17 (reference hygiene).
    lower_skill = text.lower()
    for cited in sorted(set(CITED_RE.findall(text))):
        target = folder / cited.rstrip('.,)')
        if '..' in target.relative_to(folder).parts:
            continue
        if not target.exists():
            if any((other / cited).exists() for other in siblings):
                add('NIT', 15, 'cross_skill_reference', 'cited path exists only in another skill: ' + cited[:120])
            else:
                add('MAJOR', 15, 'broken_reference', 'cited file missing: ' + cited[:120])
    files = 0
    for doc in sorted(folder.rglob('*')):
        relative = doc.relative_to(folder)
        if (doc.name == 'SKILL.md' or doc.suffix != '.md' or not doc.is_file() or doc.name.lower().startswith(('llms', 'retired-'))
                or any(p.startswith('.') or p in ARCHIVE_PARTS or p in EXEMPT_PARTS for p in relative.parts)):
            continue
        if (folder / relative.parts[0] / 'SKILL.md').exists() and len(relative.parts) > 1 and relative.parts[0] not in DOC_DIRS:
            continue  # Nested skill folders are reviewed as their own skill.
        files += 1
        if files > MAX_FILES_PER_SKILL:
            add('NIT', 17, 'file_scan_limit', 'more than %d reference documents; rest not checked' % MAX_FILES_PER_SKILL)
            break
        parts = relative.parts[:-1]
        if len(parts) > 1 or (len(parts) == 1 and parts[0] not in DOC_DIRS):
            add('MINOR', 17, 'nested', 'support file not one level under references/ etc.: ' + str(relative)[:120])
        if doc.name.lower() != 'readme.md':
            if doc.stem.lower() not in lower_skill and doc.name.lower() not in lower_skill:
                add('MINOR', 17, 'orphan_reference', 'not mentioned by SKILL.md: ' + str(relative)[:120])
            if not set(parts) & {'templates', 'assets'}:
                try:
                    ref = diag.read(doc, [root])
                except (ValueError, OSError):
                    continue
                n_lines = ref.count('\n') + 1
                if 100 < n_lines <= 5000 and not (TOC_RE.search('\n'.join(ref.splitlines()[:80])) or TOC_RE.search(ref[:400])):
                    add('MINOR', 17, 'no_contents_list', '%s has %d lines and no Contents list' % (str(relative)[:100], n_lines))
    related = ((meta.get('metadata') or {}).get('hermes') or {}).get('related_skills') if isinstance(meta.get('metadata'), dict) and isinstance((meta.get('metadata') or {}).get('hermes'), dict) else None
    return {'name': name, 'declared_name': declared, 'description': description, 'chars': chars,
            'sections': sections(body), 'related': related if isinstance(related, list) else []}


def skill_review(diag, args):
    profile = args.get('profile', 'default')
    if profile not in diag.PROFILES:
        raise ValueError('profile must be one of ' + ', '.join(diag.PROFILES))
    scope = args.get('scope', 'custom')
    if scope not in {'custom', 'all'}:
        raise ValueError('scope must be custom or all')
    wanted = args.get('skill')
    if wanted is not None and (not isinstance(wanted, str) or not re.fullmatch(r'[A-Za-z0-9_.\-가-힣]{1,80}', wanted)):
        raise ValueError('skill must be a skill folder name')
    root = diag.home(profile) / 'skills'
    if not root.is_dir():
        raise ValueError('This profile has no skills folder')
    root = diag.safe(root, [root])
    bundled = bundled_names(diag, root)
    counts = usage(diag, root)
    paths, skipped = discover(diag, root)
    all_names = {p.parent.name for p in paths}
    selected = [p for p in paths if (scope == 'all' or p.parent.name not in bundled) and (not wanted or p.parent.name == wanted)]
    if wanted and not selected:
        raise ValueError('Skill not found in this scope')
    reports, info = {}, {}
    for path in selected:
        name = path.parent.name
        findings = []
        def add(severity, check, code, detail, findings=findings):
            findings.append({'severity': severity, 'check': check, 'code': code, 'detail': detail})
        try:
            info[name] = review_one(diag, path, root, add, [p.parent for p in paths if p != path])
        except (ValueError, OSError) as exc:
            add('MAJOR', 1, 'unreadable', type(exc).__name__)
        reports[name] = findings
    # Library-wide checks over the selected set.
    by_description = defaultdict(list)
    for name, data in info.items():
        if data['description']:
            by_description[data['description'].lower()].append(name)
    for names in by_description.values():
        if len(names) > 1:
            for name in names:
                reports[name].append({'severity': 'MINOR', 'check': 'library', 'code': 'duplicate_description',
                                      'detail': 'same description as ' + ', '.join(n for n in names if n != name)[:120]})
    section_owners = defaultdict(list)
    for name, data in info.items():
        for heading, content in data['sections'].items():
            if len(content) >= 80:
                section_owners[(heading, content)].append(name)
    for (heading, _), names in section_owners.items():
        if len(names) > 2:
            for name in names:
                reports[name].append({'severity': 'NIT', 'check': 7, 'code': 'boilerplate_section',
                                      'detail': 'section "%s" identical in %d skills' % (heading[:60], len(names))})
    for name, data in info.items():
        missing = [r for r in data['related'] if isinstance(r, str) and r not in all_names]
        if missing:
            reports[name].append({'severity': 'MINOR', 'check': 'library', 'code': 'unresolved_related_skills',
                                  'detail': 'related_skills not in this profile: ' + ', '.join(missing[:5])})
    rows = []
    for name, findings in reports.items():
        findings.sort(key=lambda f: SEVERITY[f['severity']])
        use = counts.get(name, {})
        rows.append({'skill': name, 'bundled': name in bundled, 'chars': info.get(name, {}).get('chars'),
                     'usage': use or None, 'counts': dict(Counter(f['severity'] for f in findings)),
                     'findings': findings[:8], 'findings_truncated': len(findings) > 8})
    rows.sort(key=lambda r: (-(r['counts'].get('MAJOR', 0)), -((r['usage'] or {}).get('use_count') or 0), r['skill']))
    totals = Counter()
    by_code = Counter()
    for findings in reports.values():
        totals.update(f['severity'] for f in findings)
        by_code.update(f['code'] for f in findings)
    limit = 40
    return {'profile': profile, 'scope': scope, 'skills_reviewed': len(rows), 'skills_on_disk': len(paths),
            'bundled_in_manifest': len(bundled), 'symlink_or_unsafe_skipped': skipped,
            'usage_available': bool(counts),
            'summary': {'by_severity': dict(totals), 'by_code': dict(by_code.most_common(15)),
                        'skills_with_findings': sum(1 for f in reports.values() if f)},
            'skills': [r for r in rows if r['findings']][:limit],
            'clean_skills': [r['skill'] for r in rows if not r['findings']][:60],
            'skills_truncated': sum(1 for r in rows if r['findings']) > limit,
            'note': ('Mechanical checks only (upstream skill-quality-review Checks 1,4,6,7,9,10,11,12,15-existence,'
                     '17,18). Findings are review leads; nothing was modified or executed. Ordered by MAJOR count, '
                     'then use_count from .usage.json.')}
