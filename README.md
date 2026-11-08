# 스마트 엘리베이터 (Smart Elevator)

엘리베이터 배차 알고리즘 시뮬레이터와 운행 모니터링 백엔드입니다. 하드웨어 없이, 서버 안에서 엘리베이터를 시뮬레이션합니다.

## 만든 이유

첫 번째 포트폴리오 [reservation-api](https://github.com/hocastle1999/reservation-api)는 예약 API로 트랜잭션, 동시성, 락을 다뤘습니다. 이 프로젝트는 그와 겹치지 않는 백엔드 역량을 보여 주려고 만들었습니다.

- **상태 머신**: 엘리베이터는 상태(이동, 문 열림 등)와 전이 규칙이 분명한 대표적인 대상입니다.
- **스케줄링 알고리즘**: 어떤 호출을 어느 엘리베이터가 언제 처리할지는 정답이 하나가 아니라 측정해서 비교해야 하는 문제입니다.
- **실시간 처리**: 시뮬레이션 루프를 돌리면서 REST로 호출을 받고 WebSocket으로 상태를 내보냅니다.
- **데이터 분석**: 운행 기록을 쌓아 SQL로 통계를 내고 이상을 감지합니다.

## 로드맵

| 단계 | 내용 | 상태 |
|---|---|---|
| 1 | 단일 엘리베이터 상태 머신 엔진 (LOOK 방식, 결정적 시뮬레이션) | ✅ 완료 |
| 2 | 여러 대 + 배차 전략 인터페이스(Strategy 패턴) + 가장 가까운 차(Nearest Car) | ✅ 완료 |
| 3 | 실시간 서버: asyncio 시뮬레이션 루프, REST로 호출, WebSocket 상태 방송, 간단한 화면 한 페이지 | ✅ 완료 |
| 4 | 배차 알고리즘 2~3개(Nearest Car, 방향 일치·도착 예상시간 비용 기반, 목적층 예약 방식)와 교통 패턴(출근 상향 피크, 점심, 퇴근 하향 피크, 포아송 도착) 실험. 같은 시드로 평균·p95 대기시간과 이동 거리를 비교한 문서 | ✅ 완료 |
| 5 | 운행 이벤트를 MySQL에 저장하고 통계 API 제공 (윈도우 함수 등 SQL 분석) | ✅ 완료 |
| 6 | 고장 주입과 이상 감지 규칙, 점검 관리 | ✅ 완료 |
| 7 | 배포 | 🟡 준비 완료 (Render 설정, CI 기동 확인). 실제 배포는 보류 |

공개 데모의 시뮬레이션은 **모든 방문자가 하나를 공유**합니다 (3단계에서 결정, 이유는 docs/design.md 5절).

## 1단계: 시뮬레이션 엔진

`app/simulation/`에 순수 파이썬으로 만들었습니다. I/O, asyncio, 실제 시계에 의존하지 않아서 테스트가 빠르고, 같은 입력이면 항상 같은 결과가 나옵니다. 자세한 설계는 [docs/design.md](docs/design.md)에 있습니다.

- **상태 머신**: `IDLE`, `MOVING_UP`, `MOVING_DOWN`, `DOORS_OPENING`, `DOORS_OPEN`, `DOORS_CLOSING`. 진행 방향(의도)은 상태와 따로 기억합니다.
- **LOOK 운행**: 진행 방향의 호출을 먼저 처리하고(지나가는 층의 같은 방향 손님은 태움), 앞쪽에 더 없으면 방향을 바꿉니다. SCAN처럼 건물 끝까지 가지 않고, FCFS처럼 왔다 갔다 하지 않습니다.
- **고정 간격 시간 진행**: `tick(dt_ms)`. 시간은 정수 밀리초이고, tick 크기를 바꿔도 결과가 같습니다.
- **결정성**: 무작위 호출은 시드로 만든 `random.Random`으로만 만듭니다. 4단계에서 배차 알고리즘을 같은 조건으로 비교하는 기반입니다.

```python
from app.simulation import Building, Direction, Elevator

e = Elevator(Building(num_floors=10))
e.press_car_button(6)  # 카 안에서 6층
e.assign_hall_call(4, Direction.DOWN)  # 4층에서 내려가려는 손님
e.tick(60_000)  # 60초 진행
print([(s.time_ms, s.state, s.floor) for s in e.history])
# 6층에 먼저 들르고(4층의 내려가는 손님은 지나침), 돌아오면서 4층에 선다.
```

## 2단계: 여러 대와 배차 전략

- 홀 호출은 건물(`Simulation.press_hall_button`)이 받아 **배차 전략**이 고른 카에 맡기고, 카 호출은 그 카가 직접 받습니다. 1단계 엔진(LOOK)은 고치지 않고 그대로 씁니다.
- **Strategy 패턴**: `DispatchStrategy.select(call, elevators)` 하나만 구현하면 새 전략을 끼울 수 있습니다. 4단계에서 알고리즘을 같은 조건으로 바꿔 가며 비교하기 위한 구조입니다.
- **Nearest Car**: 거리만 보지 않고 진행 방향까지 따진 적합도(FS)로 고릅니다. 같은 방향으로 오는 카가 가장 좋고, 멀어지는 카는 가까워도 가장 나쁩니다.

```python
from app.simulation import Building, Direction, Simulation

sim = Simulation(Building(num_floors=10), num_elevators=2, start_floors=[1, 10])
sim.press_hall_button(3, Direction.UP)  # -> 0번 카 (1층에서 대기, 더 가까움)
sim.press_hall_button(8, Direction.DOWN)  # -> 1번 카 (10층에서 대기, 더 가까움)
sim.run_until(60_000)
print([(a.time_ms, a.call.floor, a.car) for a in sim.assignments])
```

## 3단계: 실시간 서버

- asyncio 루프 하나가 공유 시뮬레이션을 100ms마다 진행하고, WebSocket(`/ws`)으로 모든 접속자에게 상태를 방송합니다.
- REST로 호출합니다: `POST /api/hall-calls`(배차된 카 번호를 돌려줌), `POST /api/elevators/{car}/car-calls`.
- **락 없이 안전**: 루프와 핸들러가 같은 이벤트 루프 스레드에서 돌고, 그 사이에 `await`가 없어 서로 끼어들지 않습니다. 그래서 핸들러는 모두 `async def`입니다.
- **느린 접속자 격리**: 접속자마다 최신 상태 1개만 담는 큐를 둬서, 한 명이 느려도 루프와 다른 접속자는 기다리지 않습니다.
- **공유 데모 보호**: 접속자(IP)별 호출 빈도 제한(429 + `Retry-After`), 동시 접속 상한(넘으면 1013으로 닫음).

## 4단계: 승객과 배차 전략 비교

- **승객 모델**: 승객이 도착해 버튼을 누르고, 같은 방향 카에 정원까지 타고, 목적층에서 내립니다. 대기 시간과 이동 시간을 잽니다.
- **사건 단위 시간 진행**: 한 카의 탑승이 다른 카의 호출을 취소하는 등 카끼리 영향을 주므로, 모든 카를 "가장 이른 전이 시각"까지 함께 옮긴 뒤 전이를 처리합니다. tick 크기와 무관한 결과를 유지합니다.
- **배차 전략 3개**: Nearest Car, ETA 비용(맡은 정지까지 따진 도착 예상시간), 목적층 예약(같은 목적층 승객을 한 카에 모음).
- **교통 패턴**: 출근(로비→위층), 점심, 퇴근(위층→로비). 포아송 과정으로 도착.

[실험 결과](docs/experiments.md) (15층, 4대, 시드 5개):

| 상황 | 평균 대기: Nearest Car / ETA / 목적층 예약 |
|---|---|
| 출근, 분당 30명 | 346.1초 / 165.7초 / **40.8초** |
| 점심, 분당 30명 | 28.7초 / **27.2초** / 45.4초 |
| 퇴근, 분당 20명 | 25.0초 / **19.6초** / 21.4초 |

모든 상황에서 이기는 전략은 없습니다. 목적층 예약은 출근 혼잡에서 처리 용량을 크게 늘리지만, 점심과 퇴근에는 배정된 카만 탈 수 있다는 제약 때문에 오히려 불리합니다.

## 5단계: 운행 기록 저장과 SQL 통계

- **저장**: 실험 실행은 끝난 뒤 한 트랜잭션으로(`--save`), 실시간 서버는 메모리 버퍼에 모았다가 5초마다 다른 스레드에서 묶어서(write-behind) MySQL에 저장합니다. DB가 죽어도 시뮬레이션은 멈추지 않습니다.
- **통계 API**: 윈도 함수로 p95 대기 시간(`ROW_NUMBER`), 카별 상태 시간 비율(`LEAD`, 집계 위의 윈도), 시간대별 이동 평균(`ROWS BETWEEN`), 층 순위(`RANK`)를 냅니다.
- **검증**: SQL로 구한 값이 파이썬 계산과 정확히 같습니다. 4단계 실험 135회를 저장하면 `GET /api/stats/compare`가 [실험 문서](docs/experiments.md)의 27개 묶음 값을 그대로 재현합니다.
- DB는 선택 사항입니다. `DB_ENABLED`가 꺼져 있으면 기록과 통계 API만 빠지고 나머지는 그대로 동작합니다.

| API | 내용 |
|---|---|
| `GET /api/stats/runs` | 실행 목록 (`source=experiment`/`live`) |
| `GET /api/stats/runs/{id}` | 대기·이동 시간 평균, p95, 최대 |
| `GET /api/stats/runs/{id}/cars` | 카별 상태(이동, 문, 대기) 시간과 비율 |
| `GET /api/stats/runs/{id}/timeline` | 구간별 평균 대기와 이동 평균 |
| `GET /api/stats/runs/{id}/floors` | 출발 승객이 많은 층 순위 |
| `GET /api/stats/compare` | 저장된 실험의 전략 비교 |

## 6단계: 고장, 이상 감지, 점검 관리

- **고장 주입**: 문 센서 이상(닫힐 때마다 다시 열림), 층 사이 멈춤(이동 중으로 보이지만 멈춤), 모터 저하(이동 2배). 카는 고장을 알리지 않으므로 시스템이 **동작을 보고** 알아채야 합니다.
- **이상 감지 규칙**: 출발 없이 연속 재열림 10번, 다음 층까지 정상 시간의 3배, 느린 구간 연속 3번. 문 재열림 임계값은 저장된 정상 운행 기록 31만 행을 SQL로 분석해 정했습니다(정상 최대 6번).
- **자동 격리**: 심각한 이상이면 그 카를 운행에서 빼고(`OUT_OF_SERVICE`) 맡던 호출을 다른 카에 넘깁니다. 점검 요청은 태운 승객을 내려놓은 뒤 멈춥니다.
- **점검 관리**: 고장, 감지, 운행 중지, 수리, 복귀를 기록하고 SQL로 카별 가동률, 평균 복구 시간, 고장별 감지까지 걸린 시간을 냅니다(`GET /api/stats/runs/{id}/service`).
- 관리 API(고장 주입, 수리, 점검, 복귀)는 `ADMIN_TOKEN`을 설정했을 때만 열립니다.

[고장 시나리오 실험](docs/experiments.md) (4대 중 1대 고장, 15분 뒤 수리):

| 고장 | 3분 넘게 기다린 승객: 감지 끔 → 켬 | 최대 대기 | 감지까지 |
|---|---|---|---|
| 층 사이 멈춤 | 111명 → **0명** | 954초 → 136초 | 7.2초 |
| 문 센서 | 67명 → **0명** | 678초 → 154초 | 55.7초 |

## 실행 방법

Windows PowerShell, 프로젝트 루트 기준입니다. Python 3.11이 필요합니다.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

# 테스트
.\.venv\Scripts\python.exe -m pytest -q

# 린트, 포맷 검사
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .

# 배차 전략 비교 실험 (결과를 UTF-8 Markdown으로 저장)
.\.venv\Scripts\python.exe -m scripts.compare_strategies --output results.md

# (선택) 운행 기록 DB: MySQL 8.0에서 root로 scripts/init_db.sql을 한 번 실행하고
# .env.example을 .env로 복사해 비밀번호를 채운 뒤 스키마를 만든다
.\.venv\Scripts\alembic.exe upgrade head

# 실험 결과를 DB에도 저장
.\.venv\Scripts\python.exe -m scripts.compare_strategies --save --output results.md

# 서버 실행 후 브라우저에서 http://localhost:8000 (API 문서: /docs)
.\.venv\Scripts\python.exe -m uvicorn app.main:app
```

서버 설정은 환경 변수로 바꿉니다.

| 환경 변수 | 기본값 | 설명 |
|---|---|---|
| `SIM_NUM_FLOORS` | 10 | 층 수 |
| `SIM_NUM_ELEVATORS` | 3 | 카 대수 |
| `SIM_TICK_MS` | 100 | 실제 시간으로 몇 ms마다 진행할지 |
| `SIM_SIM_MS_PER_TICK` | 100 | tick 한 번에 진행할 시뮬레이션 시간 (200이면 2배속) |
| `SIM_MAX_CONNECTIONS` | 100 | WebSocket 동시 접속 상한 |
| `SIM_CALLS_PER_WINDOW` | 10 | 접속자별 호출 허용 횟수 |
| `SIM_RATE_WINDOW_MS` | 10000 | 위 횟수를 세는 기간 |
| `SIM_RECORD_FLUSH_MS` | 5000 | 운행 기록을 DB에 묶어서 저장하는 간격 |
| `ADMIN_TOKEN` | (없음) | 관리 API 토큰. 비워 두면 관리 API는 403. 요청 헤더 `X-Admin-Token`으로 보낸다 |
| `SIM_TRUSTED_PROXY_HOPS` | 0 | 앞에 있는 신뢰하는 프록시 수. 0이면 `X-Forwarded-For`를 무시 (Render는 1) |
| `DB_ENABLED` | false | 운행 기록 저장과 통계 API 사용 (`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`는 .env.example 참고) |

GitHub Actions가 push와 PR마다 같은 검사(ruff check, ruff format --check, pytest)를 실행합니다.

## 배포

`render.yaml`(Render Blueprint)로 배포합니다. Render 대시보드에서 **New → Blueprint**로 이 저장소를 고르면 설정대로 만들어집니다. 설계 이유는 [docs/design.md](docs/design.md) 9절에 있습니다.

- **무료 플랜, 싱가포르, 워커 1개**: 시뮬레이션이 메모리에 하나뿐이라 프로세스와 인스턴스를 1개로 둡니다. 무료 플랜은 15분 동안 요청이 없으면 잠들고, 깨어나면 시뮬레이션이 처음부터 시작합니다.
- **DB 없이 시작**: 운행 기록과 통계 API가 필요하면 대시보드에서 `DB_ENABLED=true`와 `DB_*` 값을 넣습니다.
- **관리 토큰**: Render가 `ADMIN_TOKEN`을 무작위로 만듭니다. 값은 대시보드의 Environment에서 봅니다.
- **프록시 뒤의 빈도 제한**: `SIM_TRUSTED_PROXY_HOPS=1`. `X-Forwarded-For`에서 프록시가 덧붙인 오른쪽 값만 써서, 방문자가 헤더를 위조해 제한을 피할 수 없게 했습니다.
- 무료 시간(월 750시간)은 워크스페이스 단위로 함께 쓰므로, 다른 무료 서비스와 다른 워크스페이스에 둡니다.

배포 후 확인할 것: `/health`, 화면의 "연결됨", 브라우저 두 개에서 같은 건물이 보이는지, 429 응답의 기준 IP가 내 공인 IP인지, 관리 API가 토큰 없이 401인지 (design.md 9절).

## 문서

- [docs/design.md](docs/design.md): 상태 머신 다이어그램, LOOK과 SCAN·FCFS 비교, 시간 처리와 결정성, 배차 전략과 Nearest Car, 실시간 서버(공유 방식, 동시성, 방송), 승객 모델과 사건 단위 시간 진행, 운행 기록 테이블과 윈도 함수 통계, 고장·감지·점검, 배포 결정
- [docs/experiments.md](docs/experiments.md): 배차 전략 비교 실험, 고장 시나리오 실험
- [docs/troubleshooting.md](docs/troubleshooting.md): 개발 중 겪은 문제와 해결 기록
