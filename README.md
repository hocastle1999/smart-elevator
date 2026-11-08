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
| 2 | 여러 대 + 배차 전략 인터페이스(Strategy 패턴) + 가장 가까운 차(Nearest Car) | 예정 |
| 3 | 실시간 서버: asyncio 시뮬레이션 루프, REST로 호출, WebSocket 상태 방송, 간단한 화면 한 페이지 | 예정 |
| 4 | 배차 알고리즘 2~3개(Nearest Car, 방향 일치·도착 예상시간 비용 기반, 목적층 예약 방식)와 교통 패턴(출근 상향 피크, 점심, 퇴근 하향 피크, 포아송 도착) 실험. 같은 시드로 평균·p95 대기시간과 이동 거리를 비교한 문서 | 예정 |
| 5 | 운행 이벤트를 MySQL에 저장하고 통계 API 제공 (윈도우 함수 등 SQL 분석) | 예정 |
| 6 | 고장 주입과 이상 감지 규칙, 점검 관리 | 예정 |
| 7 | 배포 | 예정 |

3단계에서 정할 것: 공개 데모에서 방문자마다 시뮬레이션을 따로 둘지, 하나를 공유할지(자원 제한 포함).

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
e.press_hall_button(4, Direction.DOWN)  # 4층에서 내려가려는 손님
e.tick(60_000)  # 60초 진행
print([(s.time_ms, s.state, s.floor) for s in e.history])
# 6층에 먼저 들르고(4층의 내려가는 손님은 지나침), 돌아오면서 4층에 선다.
```

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
```

GitHub Actions가 push와 PR마다 같은 검사(ruff check, ruff format --check, pytest)를 실행합니다.

## 문서

- [docs/design.md](docs/design.md): 상태 머신 다이어그램, LOOK과 SCAN·FCFS 비교, 시간 처리와 결정성
- [docs/troubleshooting.md](docs/troubleshooting.md): 개발 중 겪은 문제와 해결 기록
