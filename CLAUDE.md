# CLAUDE.md

스마트 엘리베이터: 배차 알고리즘 시뮬레이터와 운행 모니터링. 백엔드 포트폴리오용 프로젝트(2번째).
1번 프로젝트(reservation-api)가 트랜잭션과 락 중심이라면, 이 프로젝트는 실시간 처리, 상태 머신,
스케줄링 알고리즘, 데이터 분석을 보여 준다. 하드웨어 없이 서버 안의 시뮬레이션으로만 만든다.

## 목적과 작업 방식
- 사용자가 면접에서 모든 코드를 설명할 수 있어야 한다.
  - 새 개념을 도입할 때는 왜 그렇게 하는지, 다른 방법과 비교해 짧게 설명한다.
  - 기능 단위로 작게 나눠 진행하고 커밋도 그 단위로 나눈다.
- 실제로 겪은 문제는 `docs/troubleshooting.md`에 문제, 원인, 해결, 배운 점으로 남긴다.
- 설계 결정은 `docs/design.md`를 갱신한다. 상태나 전이를 바꾸면 Mermaid 다이어그램도 고친다.
- 진행 단계는 README "로드맵"을 따른다. 현재 6단계(고장, 이상 감지, 점검 관리)까지 완료.
- 사용자와의 대화와 문서는 한국어로 작성한다. 코드 식별자는 영어, 주석은 한국어로 쓴다.

## 기술 스택
- Python 3.11 (`.python-version`)
- 엔진(`app/simulation`)은 표준 라이브러리만 쓴다. 서버(`app/server`)는 FastAPI, uvicorn(WebSocket).
- DB(선택): MySQL 8.0(로컬은 Windows 서비스 `MySQL80`), SQLAlchemy 2.x **Core**(ORM 아님), PyMySQL, Alembic, pydantic-settings(`.env`의 `DB_*`)
- 테스트 클라이언트는 `httpx2` (Starlette 1.10부터 `httpx`는 deprecated)
- pytest, ruff, GitHub Actions

## 명령어 (Windows PowerShell, 프로젝트 루트 기준)
- 가상환경 Python: `.\.venv\Scripts\python.exe`
- 의존성 설치: `.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`
- 테스트: `.\.venv\Scripts\python.exe -m pytest -q`
- 서버: `.\.venv\Scripts\python.exe -m uvicorn app.main:app` (화면 http://localhost:8000). 설정은 `SIM_*` 환경 변수 (README 표)
- DB 준비(최초 1회): root로 `scripts/init_db.sql` 실행(비밀번호를 바꿔서) → `.env.example`을 `.env`로 복사 → `.\.venv\Scripts\alembic.exe upgrade head`
- 스키마 변경: `app/db/tables.py` 수정 → `.\.venv\Scripts\alembic.exe revision --autogenerate -m "설명"` → 생성 파일을 읽고 고친 뒤 `upgrade head` → `downgrade -1` → `upgrade head` 왕복, `alembic.exe check`로 모델과 DB 일치 확인
  - `alembic.ini`는 ASCII만 쓴다 (Windows에서 cp949로 읽혀 한글이 깨진다).
  - autogenerate는 CHECK 내용 변화를 못 잡는다. 직접 쓰고, 이름은 `op.f("ck_...")`로 감싼다(이름 규칙 접두어가 두 번 붙는 것 방지).
  - MySQL DDL은 롤백되지 않는다. 마이그레이션이 실패하면 `alembic current`와 실제 테이블부터 확인한다. 실패를 확인하지 않고 downgrade를 이어 실행하지 않는다 (docs/troubleshooting.md).
- 린트/포맷: `.\.venv\Scripts\ruff.exe check .` / `.\.venv\Scripts\ruff.exe format .`
- 커밋 전에는 테스트, 린트, 포맷 검사를 모두 통과시킨다. CI(`.github/workflows/ci.yml`)도 같은 검사를 한다.

## 구조
- `app/simulation/`: 시뮬레이션 엔진. 순수 파이썬.
  - I/O, asyncio, 실제 시계(`time.time`), 전역 `random`을 쓰지 않는다. 같은 입력이면 항상 같은 결과가 나와야 한다(결정성). 4단계 알고리즘 비교가 여기에 기댄다.
  - `types.py`: `Direction`, `ElevatorState`, `HallCall`, `CarCall`
  - `building.py`: `Building` (층은 1층부터 `num_floors`층까지), 호출 검증
  - `config.py`: `ElevatorConfig` (이동, 문 시간. 모두 정수 ms)
  - `elevator.py`: `Elevator` 상태 머신 (LOOK). 맡은 호출(`car_calls`, `hall_calls`)만 보고 움직이며 다른 카를 모른다. 상태 전이는 `_complete_phase`(시간 경과)와 `_on_new_call`(호출 입력) 두 곳에서만 시작된다.
  - `dispatch.py`: 배차 전략. `DispatchStrategy`(ABC)의 `select(request: CallRequest, elevators, context) -> 카 번호`를 구현한다. `NearestCar`(적합도 FS), `EtaCost`(`estimate_arrival_ms`), `DestinationDispatch`(`uses_destination=True`, 승객마다 배차). 이름으로 찾을 때는 `STRATEGIES`. 승객 단위 정보는 `DispatchContext`(Protocol, `Simulation`이 구현). 새 전략은 이 인터페이스로 추가하고 `Simulation`에 분기를 넣지 않는다. 전략도 결정적이어야 한다(동점이면 번호가 작은 카).
  - `simulation.py`: `Simulation` (여러 대, 시계 보유). 홀 호출은 `press_hall_button` → 전략 → 그 카의 `assign_hall_call`. 카 호출은 `press_car_button(car, floor)`. 배차는 호출 순간 한 번만 하고(정적), 이미 맡은 카가 있으면 다시 배차하지 않는다. 배차 기록은 `assignments`.
    - 시간은 `_advance_to`가 모든 카를 가장 이른 전이 시각까지 함께 옮긴 뒤(`elapse`) 전이를 처리한다(`complete_due_phases`). 한 카의 전이가 다른 카를 바꾸는 코드는 이 구조 안에서만 동작해야 tick 크기와 무관하다. 카를 하나씩 `tick`하지 않는다.
    - 승객: `PassengerArrival`을 `schedule`하면 도착 → 호출 → `on_state_change`(DOORS_OPEN)에서 하차·탑승. 일반 방식은 한 카가 응답하면 다른 카의 같은 홀 호출을 취소한다. 결과는 `stats()`(`RunStats`).
  - `monitor.py`: 이상 감지(`Monitor`, 규칙 `Rule`, `MonitorConfig`)와 서비스 기록(`ServiceEvent`, `ServiceKind`). `Monitor(sim)`으로 붙인다. critical이면 `sim.take_out_of_service`로 격리. 멈춤 판정 시각(`next_deadline`)은 `Simulation._advance_to`가 사건으로 처리한다(결정성).
    - 임계값(연속 재열림 10번 등)은 정상 운행 데이터로 정했다. 바꾸면 `test_no_false_alarm_in_heavy_normal_traffic`으로 오탐을 확인한다.
  - 고장: `Fault`(DOOR_SENSOR, STALL, SLOW_MOTOR). 카는 고장을 알리지 않는다. `OUT_OF_SERVICE` 카는 배차 대상이 아니고(`Simulation.is_available`), 운행에서 뺄 때 맡던 홀 호출을 다시 배차한다. 운행 중인 카가 없으면 호출을 보류했다가(`press_hall_button`이 None) 복귀 때 배차한다.
  - `passengers.py`: `Passenger`, `PassengerArrival`, `percentile`(nearest-rank), `RunStats`.
  - `traffic.py`: 무작위 호출, 교통 패턴(`PATTERNS`: up-peak, lunch, down-peak)과 포아송 도착(`poisson_passengers`). 무작위성은 이 파일에만 있고 `random.Random(seed)`를 쓴다.
- `app/server/`: 실시간 서버. 공유 시뮬레이션 하나를 모든 접속자가 본다.
  - `settings.py`: `ServerSettings` (`SIM_*` 환경 변수). 새 설정을 추가하면 README 표도 갱신한다.
  - `runner.py`: `SimulationRunner`. asyncio 루프가 `tick_ms`마다 `sim_ms_per_tick`만큼 진행하고 방송한다. 밀려도 몰아서 따라잡지 않는다.
  - `broadcast.py`: 접속자마다 크기 1 큐(최신 상태만) + 전송 태스크. 방송은 기다리지 않는다.
  - `rate_limit.py`: 접속자(IP)별 슬라이딩 윈도 로그. 서버 메모리라 서버 여러 대면 공유되지 않는다.
  - `api.py`: REST와 `/ws`. `schemas.py`: Pydantic 모델(요청은 `extra="forbid"`), 상태 스냅샷.
- `app/db/`: 운행 기록 (5단계). `config.py`(`DatabaseSettings`, `create_db_engine`, 연결마다 time_zone UTC), `tables.py`(Core 테이블, 이름 규칙), `recorder.py`(`save_run`, `LiveRecorder`), `stats.py`(윈도 함수 SQL, `text()` + 바인드 파라미터).
  - 백분위수는 nearest-rank(`ROW_NUMBER` + `COUNT(*) OVER`, `CEIL(0.95 * n)`)로 파이썬 `percentile`과 같은 정의를 쓴다. 한쪽을 바꾸면 다른 쪽도 바꾼다(테스트가 비교한다).
  - 실시간 기록은 write-behind: 루프에서는 `collect`(메모리만), 저장은 `asyncio.to_thread(write)`. 실패하면 `give_back`. 재시도는 자연키 + `ON DUPLICATE KEY UPDATE`로 멱등.
- `app/server/admin.py`: 관리 API 접근 제어. `ADMIN_TOKEN`(.env)이 없으면 403, 헤더 `X-Admin-Token`이 틀리면 401. `secrets.compare_digest`로 비교.
- `app/server/stats_api.py`: 통계 API. 핸들러는 일부러 `def`(블로킹 DB I/O를 스레드 풀에서)이고, 공유 시뮬레이션을 만지지 않는다.
- `app/main.py`: `create_app(settings, db_settings)`. DB가 꺼져 있으면(`DB_ENABLED=false`) 기록과 통계 API만 빠진다(통계 API는 503). 테스트는 설정을 바꿔 새 앱을 만든다. `app/static/index.html`: 한 페이지 화면(바닐라 JS).
- `tests/`: pytest. 공용 도우미(`stops`, `run_until_idle`, 시간 상수)는 `tests/helpers.py`.
  - `tests/conftest.py`가 import 전에 `DB_ENABLED=false`, `DB_NAME=smart_elevator_test`(`TEST_DB_NAME`)를 넣는다. DB 테스트(`tests/test_db.py`)만 `db` 픽스처로 DB를 켜고, 세션 시작 때 마이그레이션을 base → head로 다시 만든다. 이름이 `_test`로 끝나지 않는 DB는 거부한다. 저장 코드가 스스로 커밋하므로 테스트마다 `DELETE FROM simulation_runs`(CASCADE)로 정리한다.
  - CI는 MySQL 8.0 서비스 컨테이너를 띄운다(`.github/workflows/ci.yml`).
  - API 테스트는 `run_loop=False`로 앱을 만들고 `runner.tick_once()`로 시간을 진행한다. 실제 루프가 필요한 테스트만 `with TestClient(app)`로 lifespan을 돌린다.
- `scripts/fault_scenario.py`: 고장 시나리오 실험(감지 켬/끔 비교). 결과는 `docs/experiments.md`.
- `scripts/compare_strategies.py`: 배차 전략 비교 실험. `python -m scripts.compare_strategies --output 파일`. 결과 해석은 `docs/experiments.md`. 전략이나 엔진 동작을 바꾸면 다시 돌려 문서의 숫자를 갱신한다.
- `docs/design.md`: 상태 머신 다이어그램과 설계 결정

## 규칙
- 시간은 정수 밀리초다. `float` 시간을 도입하지 않는다(부동소수점 오차로 tick 크기에 따라 결과가 달라짐).
- `tick(dt)` 결과는 tick 크기와 무관해야 한다. 시간 처리 코드를 바꾸면 `test_large_tick_equals_many_small_ticks`, `test_result_does_not_depend_on_tick_size`가 통과하는지 본다.
- 판단 로직이 `set` 순회 순서에 기대면 안 된다(홀 호출 해시는 실행마다 다를 수 있음). 방향은 `(UP, DOWN)` 같은 고정 순서로 확인한다.
- 시뮬레이션을 만지는 FastAPI 핸들러와 의존성은 반드시 `async def`다. `def`는 스레드 풀에서 돌아 루프와 동시에 상태를 바꾼다(락이 없다). tick과 호출 처리 사이에 `await`를 넣지 않는다.
- 파일은 `encoding="utf-8"`로 연다. Windows 기본 인코딩(cp949)으로 쓰면 한글이 깨진다 (출력 리디렉션 `>`도 마찬가지).
- 배차 전략이 엔진 상태(호출 집합 등)를 읽을 때는 그 상태가 언제 바뀌는지 확인한다. 홀 호출은 문을 열 때 지워진다 (docs/troubleshooting.md).
- 상태를 바꾸면 콜백(`on_state_change`)이 바로 불린다. "상태를 바꾸고 나서 초기화"하면 콜백이 막 기록한 값을 지울 수 있다. 초기화를 먼저 한다 (docs/troubleshooting.md).
- 새 상태 전이를 추가하면 그 전이를 직접 일으키는 테스트를 함께 쓴다.
- 오래 도는 서버에서 쌓이기만 하는 리스트를 만들지 않는다. 엔진의 `history`, `assignments`, `service_log`는 서버 tick마다 비운다 (docs/troubleshooting.md).
- 인덱스를 추가하면 `EXPLAIN`으로 실제로 쓰이는지 확인한다. 윈도 함수의 정렬에는 인덱스가 쓰이지 않았다 (docs/troubleshooting.md).
- 비밀 정보는 `.env`에만 둔다. 코드, 문서, 커밋에 넣지 않는다. 새 DB 설정을 추가하면 `.env.example`, README 환경 변수 표, CI의 `env`도 갱신한다.
- 의존성을 추가하면 버전을 고정해 `requirements.txt`(실행용) 또는 `requirements-dev.txt`(개발용)에 적는다.
- 커밋 메시지는 `feat: ...`, `fix: ...`, `docs: ...`, `test: ...`, `chore: ...` 형식, 내용은 한국어.
