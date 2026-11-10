# pads-mcp

[English](README.md)

AI 어시스턴트(Claude Code, Codex, GitHub Copilot)가 PADS Logic(회로도)와 PADS Layout / PADS Router(PCB) 9.x를
COM 자동화로 다루게 해 주는 MCP 서버입니다.

- **회로도**: 부품 배치, 와이어 연결, 전원/GND 심볼, 넷 라벨.
- **Agent 배선(PCB)**: AI 에이전트가 패드와 장애물 정보를 보고 층, 꺾이는 점, 비아, 선폭까지 직접 경로를 정합니다.
  서버는 그 경로를 그대로 설계에 넣습니다. 배선은 모두 PADS Layout에서 하며, 자동 배선기는 쓰지 않습니다.
  DRC는 선택 사항이며 PADS Router 검사기를 씁니다(PADS Router 라이선스 필요).
- **규칙과 임피던스**: 넷 클래스, 선폭/간격/고속 규칙, 적층 구조, 목표 임피던스에 맞는 선폭 계산.

## 동작 방식

```
AI 클라이언트 ⇄ pads-mcp (Python, stdio) ⇄ COM ⇄ PADS Logic / PADS Layout / PADS Router
```

| 작업 | 방법 |
|---|---|
| 조회 (부품, 핀, 패드, 넷, 규칙, 적층) | COM API (`PowerLogic.Application`, `PowerPCB.Application`) |
| 라이브러리 부품 배치, 이동, 회전, 속성 | COM API |
| **회로도 와이어, 전원/GND 심볼, 넷 라벨, 부품 복사** | COM API 없음 → **ASCII 라운드트립** (내보내기 → 편집 → 빈 문서로 가져오기 → 저장) |
| **PCB 트레이스·비아, 넷 클래스, 규칙, 적층** | COM API 없음 → PADS Layout에서도 같은 **ASCII 라운드트립** |
| PCB DRC (선택, PADS Router 라이선스 필요) | PADS Router COM (`BlazeRouter.Application`), 저장된 PCB의 복사본에서 실행 |
| 시각 확인 | 창 캡처 (`pads_screenshot`) |

## Agent 배선 흐름 (PCB)

1. `layout_overview`로 보드 외곽, 층, 적층, 비아 종류, 넷 클래스와 규칙, 넷별 미배선 연결 수를 봅니다.
2. `layout_get_net` / `layout_region`으로 핀(패드 층과 크기)과 경로 주변의 기존 트레이스·비아를 봅니다.
3. 모델이 경로를 정해 `layout_route`를 호출합니다. **여러 연결을 한 번에 묶어서** 보냅니다.
   ```json
   {"routes": [
     {"from": "J1.3", "to": "R3.1", "layer": "bottom",
      "path": [[31.0, 144.8], [31.0, 139.8], [35.8, 139.8]]},
     {"from": "J1.2", "to": "R2.1", "layer": "top",
      "path": [[33.6, 146.8], [33.6, 142.8, "bottom"]]}
   ]}
   ```
   - 단위는 mm이고, y는 위쪽이 +입니다.
   - 패드 중심 좌표는 자동으로 붙습니다.
   - `[x, y, "bottom"]`은 그 점에 비아를 두고 해당 층으로 넘어간다는 뜻입니다.
   - `width`를 생략하면 넷 클래스의 `REC_TRACK_WIDTH`를 씁니다.
4. 새 배선을 `layout_region`과 `pads_screenshot(app="layout", region=[x1, y1, x2, y2])`로 확인하고 다음 묶음을 배선합니다.
5. 배선이 끝나면 `layout_drc`(PADS Router 검사기)를 한 번 실행하고, 새 배선의 오류를 고칩니다.
   PADS Router 라이선스가 없으면 PADS Layout의 Tools > Verify Design으로 검사합니다.

PCB를 한 번 편집할 때마다 설계 전체를 다시 가져옵니다. 부품 160개 보드 기준으로 30~60초가 걸리니 배선을 묶어서 보내세요.

## 안전장치

- `logic_open`은 회로도를 `work\` 폴더에 **복사**해서 엽니다. 원본은 건드리지 않습니다.
- PCB 편집은 PADS Layout에 열려 있는 파일에 적용됩니다.
- 회로도와 PCB 편집은 모두 **원자적 트랜잭션**입니다.
  - 편집 전에 파일을 `work\history\`에 백업합니다.
  - PADS가 거부하거나 내용이 바뀌면(부품·넷 수가 달라지는 경우 등) 아무것도 저장하지 않고 원래 파일을 다시 엽니다.
  - `logic_undo` / `layout_undo`로 마지막 백업을 복원합니다.
- DRC는 잠금 없이 연 **복사본**에서만 실행되므로 원본 파일을 직접 건드리지 않습니다.
- ASCII 가져오기 중 뜨는 `Font Replacement` 대화상자는 자동으로 응답합니다. 다른 대화상자는 `pads_dialogs` / `pads_click_dialog`로 처리합니다.
- PADS는 한 번에 하나의 AI 클라이언트만 조작해야 합니다. COM이 실행 중인 PADS 하나에 붙기 때문입니다.

## 도구

**공통**: `pads_status`, `pads_screenshot`, `pads_dialogs`, `pads_click_dialog`

**PADS Logic (회로도, mil 단위)**
- 파일: `logic_open`, `logic_new`, `logic_save`, `logic_export` (ascii/netlist), `logic_import_ascii`, `logic_undo`
- 조회: `logic_status`, `logic_list_parts`, `logic_get_part`, `logic_list_nets`, `logic_get_net`, `logic_search_library`
- 부품: `logic_add_sheet`, `logic_add_part`, `logic_move_part`, `logic_delete_part`, `logic_rename_part`, `logic_set_attribute`
- **그리기**: `logic_draw` (부품과 넷을 한 번에), `logic_wire` (connect / power / label / disconnect / net)

**PADS Layout (PCB, mm 단위)**
- 파일: `layout_status`, `layout_open`, `layout_save`, `layout_export` (ascii/netlist/rules), `layout_undo`
- 조회: `layout_overview`, `layout_list_components`, `layout_get_component`, `layout_list_nets`, `layout_get_net`, `layout_region`
- **배선**: `layout_route`, `layout_unroute`, `layout_drc` (PADS Router 검사기, 선택)
- 규칙: `layout_get_rules`, `layout_set_rules`, `layout_stackup`, `layout_set_stackup`, `layout_impedance`
- 배치: `layout_move_component`, `layout_select`

### logic_draw 예시

```json
{
  "sheet": "POWER2",
  "parts": [
    {"refdes": "U100", "clone_from": "U6", "x": 3000, "y": 6000},
    {"refdes": "C100", "clone_from": "C3", "x": 1900, "y": 5900, "rotate": 270,
     "attributes": {"Value": "CC2012/10u/K"}}
  ],
  "nets": [
    {"name": "5V",  "pins": ["U100.VIN", "C100.1"], "style": "wire"},
    {"name": "GND", "pins": ["U100.GND", "C100.2"], "style": "power"}
  ]
}
```
- `clone_from`: 설계에 이미 있는 부품을 복사합니다. 라이브러리가 필요 없고 속성을 덮어쓸 수 있습니다.
- `clone_from` 없이 쓰면 PADS 라이브러리의 `part_type`을 배치합니다.
- `style`: `wire` | `label` | `power` | `auto`.

### layout_set_rules 예시

```json
{
  "net_classes": {"USB": ["USB_DP", "USB_DM"]},
  "rules": [
    {"target": "DEFAULT", "auto_route": true, "width": {"min": 0.2, "rec": 0.25, "max": 1.0}, "clearance": 0.2},
    {"target": "USB", "width": 0.3, "clearance": 0.2, "match_tolerance": 0.5, "impedance_min": 81, "impedance_max": 99}
  ]
}
```

## 설치 (팀원용)

### 0. 준비 (PC마다 한 번)

1. **PADS 9.x**를 설치하고, PADS Logic과 PADS Layout을 한 번씩 실행합니다. 이때 COM이 등록됩니다.
2. **uv**를 설치합니다. Python 실행 도구이며 Python도 함께 설치해 줍니다.
   ```powershell
   winget install --id=astral-sh.uv -e
   ```
3. **Git**을 준비합니다. 비공개 저장소라면 한 번 로그인해서 Git이 저장소를 읽을 수 있게 합니다.
   ```powershell
   gh auth login
   gh auth setup-git
   ```
4. 설치를 확인합니다. 첫 실행은 의존성을 내려받느라 10~30초 걸립니다.
   ```powershell
   uvx --from git+https://github.com/BlessingQ/pads-mcp@latest pads-mcp --check
   ```
   PADS Logic / Layout이 `OK`로 나오고 마지막에 `ready`가 나오면 됩니다. PADS Router는 `layout_drc`에만 필요합니다.

`uvx`는 Python과 의존성을 담은 격리 환경을 uv 캐시(`%LOCALAPPDATA%\uv\cache`)에 만들고 그 안에서 서버를 실행합니다.
따로 설치되는 것이 없고 venv나 pip 작업도 필요 없습니다. `uv cache clean`으로 지우면 다음 실행 때 다시 만들어집니다.

**버전.** `@latest`는 항상 최신 릴리스를 가리키는 브랜치입니다.
`uvx`는 클라이언트가 서버를 시작할 때마다 GitHub을 확인하므로, 새 릴리스가 나오면 설정을 바꾸지 않아도 클라이언트를 다시 시작한 뒤부터 새 버전이 쓰입니다.
특정 릴리스에 머물려면 `latest` 대신 태그를 씁니다(예: `@v0.2.0`).
어느 쪽이든 GitHub에 접속할 수 있어야 서버가 시작됩니다.
지금 쓰는 버전은 위 확인 명령에서 `--check` 대신 `--version`을 붙이면 나옵니다.

### Claude Code

**방법 A (추천): 플러그인.** MCP 서버와 PCB 설계 규칙 스킬(`skills/`)을 함께 설치합니다.
```text
/plugin marketplace add BlessingQ/pads-mcp
/plugin install pads@pads-mcp
```
셸에서는 `claude plugin marketplace add BlessingQ/pads-mcp`, 그다음 `claude plugin install pads@pads-mcp`를 실행합니다.
저장소 자체가 마켓플레이스(`.claude-plugin/marketplace.json`)이므로 따로 올릴 것은 없습니다.

**방법 B: MCP 서버만 설치.**
```powershell
claude mcp add --scope user --transport stdio pads -- uvx --from git+https://github.com/BlessingQ/pads-mcp@latest pads-mcp
```
스킬도 쓰려면 `skills\pads-pcb-design` 폴더를 `%USERPROFILE%\.claude\skills\`에 복사합니다.
`claude mcp list`에서 `pads ... ✔ Connected`가 보이거나, 세션 안에서 `/mcp`로 확인할 수 있으면 됩니다.

### OpenAI Codex (CLI / IDE 확장 / 앱)

`%USERPROFILE%\.codex\config.toml`에 다음을 추가합니다.
```toml
[mcp_servers.pads]
command = "uvx"
args = ["--from", "git+https://github.com/BlessingQ/pads-mcp@latest", "pads-mcp"]
startup_timeout_sec = 120   # 기본값 10초는 uvx 첫 다운로드에 부족
tool_timeout_sec = 600      # 기본값 60초, PCB 편집 + DRC는 1분 이상 걸릴 수 있음
```
`codex mcp add pads -- uvx --from git+https://github.com/BlessingQ/pads-mcp@latest pads-mcp`로 추가해도 됩니다.
이 명령에는 타임아웃 옵션이 없으므로, 추가한 뒤 위의 타임아웃 두 줄을 `config.toml`에 직접 넣어 주세요.
- 스킬: `skills\pads-pcb-design` 폴더를 `%USERPROFILE%\.agents\skills\`(사용자 전체) 또는 저장소의 `.agents\skills\`에 복사합니다.
- `codex mcp list` 또는 세션 안의 `/mcp`로 확인합니다.

### VS Code + GitHub Copilot

아래 두 설정 파일 중 하나를 쓰면 됩니다.
- **Copilot 공용 설정** (현재 "Add Server" 기본 방식): `%USERPROFILE%\.copilot\mcp-config.json`
  ```json
  { "mcpServers": { "pads": { "command": "uvx",
      "args": ["--from", "git+https://github.com/BlessingQ/pads-mcp@latest", "pads-mcp"] } } }
  ```
- **VS Code 형식**: 명령 팔레트 → **MCP: Open User Configuration**, 또는 작업 공간의 `.vscode/mcp.json`
  ```json
  { "servers": { "pads": { "type": "stdio", "command": "uvx",
      "args": ["--from", "git+https://github.com/BlessingQ/pads-mcp@latest", "pads-mcp"] } } }
  ```

그다음:
- Copilot Chat에서 **Agent** 에이전트로 사용하고, Tools 목록에서 `pads`가 켜져 있는지 확인합니다.
- 스킬: `skills\pads-pcb-design` 폴더를 `%USERPROFILE%\.copilot\skills\` 또는 작업 공간의 `.github\skills\`에 복사합니다.
- Copilot Business/Enterprise는 조직 관리자가 **MCP servers in Copilot** 정책을 켜야 합니다. 기본값은 꺼짐입니다.

### 설정

| 환경 변수 | 의미 |
|---|---|
| `PADS_MCP_WORKDIR` | 회로도 복사본, 임시 ASCII, 백업을 두는 작업 폴더입니다. 소스 폴더에서 실행하면 `<repo>\work`, 그 외에는 `%USERPROFILE%\Documents\pads-mcp\work`가 기본값입니다. |

### 제거

설정한 클라이언트마다 등록을 지운 뒤 그 클라이언트를 다시 시작합니다.

| 클라이언트 | 서버 제거 | 스킬 제거 |
|---|---|---|
| Claude Code (플러그인) | `claude plugin uninstall pads@pads-mcp` 후 `claude plugin marketplace remove pads-mcp` | 플러그인과 함께 제거됨 |
| Claude Code (MCP 서버만) | `claude mcp remove pads --scope user` | `%USERPROFILE%\.claude\skills\pads-pcb-design` 삭제 |
| OpenAI Codex | `codex mcp remove pads`, 또는 `%USERPROFILE%\.codex\config.toml`에서 `[mcp_servers.pads]` 블록 삭제 | `%USERPROFILE%\.agents\skills\` 또는 저장소 `.agents\skills\`에서 `pads-pcb-design` 삭제 |
| VS Code + Copilot | `mcp-config.json` 또는 `mcp.json`에서 `pads` 항목 삭제 | `%USERPROFILE%\.copilot\skills\` 또는 `.github\skills\`에서 `pads-pcb-design` 삭제 |

선택 정리:
- `uv cache clean pads-mcp`로 캐시에 남은 빌드를 지웁니다. 디스크만 차지할 뿐이고, 필요하면 `uvx`가 다시 만듭니다.
- 작업 폴더(`%USERPROFILE%\Documents\pads-mcp\work` 또는 `PADS_MCP_WORKDIR`)는 위 명령으로 지워지지 않습니다.
  회로도 복사본과 수정 전 백업이 들어 있으니, 필요 없을 때 직접 지웁니다.

## 설계 지식은 어디에 두나 (MCP vs 스킬)

| 내용 | 위치 | 이유 |
|---|---|---|
| PADS 조작 방법, 반드시 지켜야 하는 PADS 규칙(핀당 와이어 1개, SMD 패드는 자기 층에만 있음 등) | MCP 코드 + 서버 설명 | 올바른 동작에 필요하고 도구와 함께 바뀜 |
| 배치·배선 규칙, 주의사항, 회사 기준(제조사 한계, 적층, 절연 거리) | **스킬** `skills/pads-pcb-design/SKILL.md` | 회사·제품마다 다르고, 마크다운이라 누구나 고칠 수 있으며, 필요할 때만 읽힘 |

Claude Code 플러그인은 둘을 함께 설치합니다. Codex와 Copilot은 같은 `SKILL.md`를 복사해서 씁니다.

## 릴리스 (관리자)

1. `pyproject.toml`, `src/pads_mcp/__init__.py`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`의 버전을 올립니다.
2. 커밋한 뒤 `git tag vX.Y.Z && git push && git push --tags`를 실행합니다.
3. `git push origin main:latest`로 `latest`를 새 릴리스로 옮깁니다. `@latest` 사용자는 클라이언트를 다시 시작하면 새 버전을 받습니다.
   릴리스하지 않은 커밋은 `latest`에 올리지 않습니다. 설치 예시는 고칠 필요가 없습니다.

- `work\`(설계 복사본, 백업)와 설계 파일(`*.sch`, `*.pcb`, `*.asc`)은 `.gitignore`로 제외됩니다.

## 개발 환경

```powershell
git clone https://github.com/BlessingQ/pads-mcp; cd pads-mcp
uv venv .venv
uv pip install --python .venv\Scripts\python.exe -e .
claude mcp add --scope user pads -- "$PWD\.venv\Scripts\python.exe" -m pads_mcp.server
```
- 로컬 소스로 uvx 설치를 시험하려면 `uvx --from . pads-mcp --check`를 실행합니다.
- 플러그인을 설치하지 않고 시험하려면 `claude --plugin-dir . mcp list`를 실행합니다.

| `tools\` 파일 | 용도 |
|---|---|
| `mcp_call.py` | 실제 MCP stdio 프로토콜로 도구 호출 (E2E 테스트) |
| `typelib_dump.py` | COM 서버를 띄우지 않고 메서드 목록 확인 (`typelib_dump.py BlazeRouter.Application`) |
| `validate_geometry.py`, `check_roundtrip.py`, `test_route.py` | 회로도 파서 / 기하 / 라우터 테스트 |
| `sheet_dump.py`, `pin_degree.py`, `list_windows.py` | 진단 |

## 이 도구가 기대는 PADS 동작 (PADS 9.3에서 확인)

**회로도 (PADS Logic ASCII)**
- 핀 하나에는 와이어가 하나만 붙고, 갈라지는 곳은 tie dot(와이어 3개)을 거칩니다.
- 핀 데칼은 그 핀을 쓰는 CAE 데칼보다 먼저 정의돼야 합니다.
- 비어 있는 `*PART*` 섹션은 빈 줄 3개로 시작합니다.

**PCB (PADS Layout ASCII)**
- `*ROUTE*`는 넷을 2핀 연결 단위로 저장합니다 (`핀A 핀B` + 점 `X Y 층 폭 플래그 [비아]`).
  - 층 0은 미배선, 65는 끝점입니다.
  - 비아 점에는 비아 이후의 층이 적힙니다.
- 한 넷의 연결은 **트리 순서**로 적혀 있어야 합니다. 아니면 PADS가 "Connection not physically tied to the signal tree" 오류를 내고 거기서 읽기를 멈춥니다.
- 배선만 담은 ASCII를 열린 설계에 가져오면 "duplicate connection"으로 거부됩니다. 그래서 편집할 때마다 설계 전체를 빈 문서로 다시 가져옵니다.
- 규칙은 `*MISC*` → `RULES_SECTION`에 있습니다.
  - `NET_CLASS DATA`
  - `RULE_SET`의 `CLEARANCE_RULE` / `ROUTE_RULE` / `HIGH_SPEED_RULE`
  - `AUTO_ROUTE` 줄이 없으면 N입니다.
- PADS Layout COM으로는 트레이스나 비아를 만들 수 없습니다.
- PADS Router COM은 배선(`Router.Run`, 선택 대상 `Route()`, `LCTune`)과 검사(`Checker`)는 되지만, 규칙은 설정할 수 없습니다.
  pads-mcp는 PADS Router로 배선하지 않습니다. PADS Router는 `layout_drc`에서만 실행되며, PADS Router 라이선스(`blzshell`)가 필요합니다.

## 알려진 한계

- PCB 편집은 한 번에 30~60초 걸립니다(설계 전체 재가져오기). 연결을 `layout_route` 한 번에 묶어 보내세요.
- 배선은 핀과 핀 사이 경로만 지원합니다. 트레이스 중간에서 갈라지는 T 분기와 동박 채우기(pour)는 아직 지원하지 않습니다.
- 임피던스는 근사식(±5~10 %)으로 계산합니다. 최종 값은 제조사에서 확인하세요.
- 회로도: 넷 라벨 글자가 심볼에서 조금 떨어져 보일 수 있고, `logic_move_part`는 붙어 있던 와이어를 다시 그리지 않습니다.

## 라이선스

[MIT](LICENSE)
