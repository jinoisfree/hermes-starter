# Hermes Starter

공개 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)에 역할별 설정을 더하는 시작 틀입니다.
개인 설정·기억·대화·계정 연동 코드는 포함하지 않습니다. Python 3와 bash가 필요합니다.

| slug | 표시 이름 | 특징 | 필요한 서비스 |
|---|---|---|---|
| llm-wiki | LLM 위키 | 근거를 모아 재사용할 지식으로 정리 | Slack, 모델 제공자, 위키 저장소(선택) |
| personal-secretary | 개인비서 | 개인 일정·메일과 AI 아이디어를 정리 | Slack, 모델 제공자, 개인 메일·일정(선택) |
| sns-manager | SNS 담당 | SNS 초안과 소식을 살피는 담당 | Slack, 모델 제공자, SNS 계정(선택) |
| system-doctor | 시스템 관리·진단 | 읽기 전용 점검으로 오류 원인을 좁힘 | Slack, 모델 제공자, 로컬 진단 도구(선택) |
| work-assistant | 회사업무 담당 | 업무 자료·마감·회신 대기를 한데 정리 | Slack, 모델 제공자, 회사 메일·일정(선택), Notion(선택) |

1. [공식 설치 안내](https://hermes-agent.nousresearch.com/docs/getting-started/quickstart/)에 따라 공개 Hermes를 설치하고 `hermes setup`으로 모델을 연결합니다.
2. `bash install.sh --list`로 필요한 역할을 고릅니다. 기본 역할은 루트, 나머지는 `profiles/<slug>`에 둡니다. 각 홈은 독립 게이트웨이로 실행하고 같은 토큰을 중복 실행하지 않습니다.
3. [공식 Slack 연결 안내](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/slack/)를 참고해 역할마다 `agents/<slug>/slack-manifest.json`으로 Slack 앱을 만듭니다. Socket Mode를 켜고 워크스페이스에 설치한 뒤 **Bot User OAuth Token**과 앱 수준 토큰(`connections:write`)을 발급합니다. 사용자 토큰과 다릅니다.
4. `.env.example`에서 선택한 역할의 변수 이름을 확인합니다. 설치 후 각 홈에 `.env`를 직접 만들고 토큰과 허용 사용자 정보를 넣습니다. 비밀 값을 저장소에 넣지 않습니다.
5. 설치 폴더를 고르고 실행합니다. 미리 보기는 `--dry-run`을 더합니다.

```sh
bash install.sh --agents llm-wiki,personal-secretary --home ~/.hermes --label-prefix com.example.hermes
```

`--default <slug>`로 기본 역할을 바꿀 수 있습니다. 생략하면 선택한 LLM 위키, 없으면 첫 번째입니다.
인자 없이 실행하면 번호를 고릅니다. 비대화형 실행에서는 `--agents`가 필요합니다.
추가 설치도 같은 명령을 사용합니다. 기존 파일은 덮어쓰지 않고 협업 목록은 새 번호의 조각 파일로 다시 계산합니다.
기존 기본 역할을 옮기려면 새 홈을 사용합니다. 기존 수동 설정 홈에 설치할 때는 먼저 별도 홈에서 검토하세요.

6. 설치된 각 홈에서 `HERMES_HOME=<각 홈> hermes setup`으로 자리표시와 모델·도구를 설정합니다. 필요한 경우 `hermes slack manifest --agent-view --write`로 최신 공식 매니페스트를 별도로 생성해 사용하세요. [프로필 공식 안내](https://hermes-agent.nousresearch.com/docs/user-guide/profiles/)를 확인합니다. `.starter/launchd`의 실행 항목 틀에 Hermes 실행 파일 위치를 채워 검토한 뒤 직접 등록합니다. 설치기는 `launchctl`을 실행하지 않습니다. 동일 홈을 두 프로세스가 사용하지 않도록 합니다.

추가 설치의 `.starter/collaboration/<번호>.json`은 선택한 상대만 담는 **안내 조각**입니다.
설정이나 SOUL을 자동으로 고치지 않으며, 협업 도구·연결은 직접 구현해야 합니다.
공통 플러그인은 quiet-logs만 포함합니다.
SNS 자동 DM·게시·감시, 개인 메일·일정·문서 연동과 예약 실행은 기본으로 꺼져 있으며 직접 연결해야 합니다.
라이선스는 아직 정하지 않았습니다. 재배포 조건은 확정 후 안내합니다.

이 저장소는 생성 결과입니다. 직접 수정하거나 개인 설정을 복사하지 말고 생성기로 다시 만드세요.
