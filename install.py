#!/usr/bin/env python3
"""Install selected templates without replacing user files or starting services."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent


def agents():
    return {p.parent.name: json.loads(p.read_text()) for p in sorted((ROOT / 'agents').glob('*/agent.yaml'))}


def table(items):
    print('slug | 표시 이름 | 특징 | 필요한 서비스')
    for slug, a in items.items():
        print(' | '.join([slug, a['display_name'], a['tagline'], ', '.join(a['services'])]))


def existing(home):
    records = sorted((home / '.starter/receipts').glob('*.json'))
    return json.loads(records[-1].read_text()) if records else {'agents': [], 'default': None, 'destinations': {}}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--list', action='store_true')
    p.add_argument('--agents')
    p.add_argument('--default')
    p.add_argument('--home', type=Path, default=Path(os.environ.get('HERMES_HOME', '~/.hermes')).expanduser())
    p.add_argument('--label-prefix', default='com.example.hermes')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args(argv)
    available = agents()
    if args.list:
        table(available)
        return 0
    if not args.agents:
        table(available)
        if not sys.stdin.isatty():
            p.error('비대화형 실행에는 --agents가 필요합니다')
        print('설치할 번호를 쉼표로 구분하세요:')
        for i, slug in enumerate(available, 1):
            print(f'{i}. {slug}')
        try:
            args.agents = ','.join(list(available)[int(n.strip()) - 1] for n in input().split(',') if int(n.strip()) > 0)
        except (ValueError, IndexError):
            p.error('잘못된 선택입니다')
    selected = list(dict.fromkeys(s.strip() for s in args.agents.split(',')))
    if any(s not in available for s in selected):
        p.error('알 수 없는 에이전트입니다')
    if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9.-]*', args.label_prefix):
        p.error('실행 항목 접두사가 올바르지 않습니다')
    home = args.home.expanduser().resolve()
    if home == ROOT or ROOT in home.parents:
        p.error('시작 틀 저장소 밖의 홈을 고르세요')
    previous = existing(home)
    all_selected = list(dict.fromkeys(previous['agents'] + selected))
    default = args.default or previous['default'] or ('llm-wiki' if 'llm-wiki' in selected else selected[0])
    if default not in all_selected or not available[default]['can_be_default']:
        p.error('기본 에이전트는 설치 목록 안에서 고르세요')
    if previous['default'] and previous['default'] != default:
        p.error('기존 기본 에이전트 이동은 새 홈에서 하세요')
    if any(s not in available for s in all_selected):
        p.error('기존 설치 목록을 확인하세요')
    destinations = dict(previous['destinations'])
    planned = []
    for slug in selected:
        destination = home if slug == default else home / 'profiles' / slug
        destinations[slug] = str(destination.relative_to(home))
        source = ROOT / 'agents' / slug
        for name in ['profile.yaml', 'config.yaml', 'SOUL.md', 'slack-manifest.json', 'README.md']:
            planned.append((destination / name, (source / name).read_bytes()))
        a = available[slug]
        env = '# ' + a['display_name'] + '\n' + ''.join(key + '=\n' for key in a['env'])
        planned.append((destination / '.env.example', env.encode()))
        for plugin in a['plugins']:
            for f in sorted((ROOT / 'plugins' / plugin).rglob('*')):
                if f.is_file():
                    planned.append((destination / 'plugins' / plugin / f.relative_to(ROOT / 'plugins' / plugin), f.read_bytes()))
        for launchd in a['launchd']:
            content = (ROOT / launchd).read_text()
            for key, value in {'HOME': str(Path.home()), 'HERMES_HOME': str(destination), 'LABEL_PREFIX': args.label_prefix}.items():
                content = content.replace('{{' + key + '}}', escape(value))
            planned.append((home / '.starter/launchd' / (args.label_prefix + '.' + slug + '.plist.template'), content.encode()))
    collaborations = {}
    for slug in all_selected:
        collaborations[slug] = []
        for peer in available[slug]['works_with']:
            if peer['slug'] in all_selected:
                collaborations[slug].append(peer)
            else:
                print(f"{slug} → {peer['slug']}: 상대가 없어 꺼 둠")
    # Append-only fragments: user config/SOUL and previous installation records are never edited.
    count = len(list((home / '.starter/receipts').glob('*.json')))
    sequence = f'{count + 1:06d}'
    state = {'agents': all_selected, 'default': default, 'destinations': destinations}
    planned += [(home / '.starter/receipts' / (sequence + '.json'), (json.dumps(state, indent=2) + '\n').encode()),
                (home / '.starter/collaboration' / (sequence + '.json'), (json.dumps(collaborations, ensure_ascii=False, indent=2) + '\n').encode())]
    for path, body in planned:
        # Reject symlinked ancestors even when the leaf does not yet exist.
        if any(parent.is_symlink() for parent in [path, *path.parents] if parent != home.parent):
            p.error('심볼릭 링크가 있는 설치 경로는 사용할 수 없습니다')
        rel = path.relative_to(home)
        if path.exists():
            print(f'유지: {rel}')
        elif args.dry_run:
            print(f'예정: {rel}')
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as handle:
                handle.write(body)
            print(f'설치: {rel}')
    print('기존 파일 덮어쓰기 없음. 협업 조각은 .starter/collaboration의 마지막 번호를 확인하세요.')
    print('다음: 각 홈에서 .env.example을 참고해 .env를 만들고 hermes setup으로 모델을 정하세요.')
    print('launchd 틀의 HERMES_EXECUTABLE을 채워 직접 검토·등록하세요. launchctl은 실행하지 않았습니다.')
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError):
        print('설치를 중단했습니다. 파일 권한과 설치 기록을 확인하세요.', file=sys.stderr)
        sys.exit(1)
