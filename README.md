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
| 4 | 배차 알고리즘 2~3개(Nearest Car, 방향 일치·도착 예상시간 비용 기반, 목적층 예약 방식)와 교통 패턴(출근 상향 피크, 점심, 퇴근 하향 피크, 포아송 도착) 실험. 같은 시드로 평균·p95 대기시간과 이동 거리를 비교한 문서 | 예정 |
| 5 | 운행 이벤트를 MySQL에 저장하고 통계 API 제공 (윈도우 함수 등 SQL 분석) | 예정 |
| 6 | 고장 주입과 이상 감지 규칙, 점검 관리 | 예정 |
| 7 | 배포 | 예정 |

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

GitHub Actions가 push와 PR마다 같은 검사(ruff check, ruff format --check, pytest)를 실행합니다.

## 문서

- [docs/design.md](docs/design.md): 상태 머신 다이어그램, LOOK과 SCAN·FCFS 비교, 시간 처리와 결정성, 배차 전략과 Nearest Car, 실시간 서버(공유 방식, 동시성, 방송)
- [docs/troubleshooting.md](docs/troubleshooting.md): 개발 중 겪은 문제와 해결 기록
