"""배차 전략 비교 실험.

같은 시드로 만든 같은 승객 목록을 전략마다 똑같이 넣고 결과를 비교한다. 엔진이 결정적이라
결과 차이는 오직 전략 차이에서 나온다.

실행 (프로젝트 루트):
    .\\.venv\\Scripts\\python.exe -m scripts.compare_strategies
    .\\.venv\\Scripts\\python.exe -m scripts.compare_strategies --rates 10 20 --seeds 3

결과는 Markdown 표로 출력한다. docs/experiments.md에 정리한다.
"""

import argparse
import time
from dataclasses import dataclass

from app.simulation import STRATEGIES, Building, Passenger, Simulation
from app.simulation.passengers import percentile
from app.simulation.traffic import PATTERNS, poisson_passengers


@dataclass
class Result:
    passengers: list[Passenger]
    floors_traveled: int
    stops: int
    runs: int


def run(
    strategy: str, pattern: str, rate: float, seeds: list[int], floors: int, cars: int, minutes: int
) -> Result:
    building = Building(num_floors=floors)
    result = Result([], 0, 0, 0)
    for seed in seeds:
        arrivals = poisson_passengers(building, PATTERNS[pattern], rate, minutes * 60_000, seed)
        sim = Simulation(building, num_elevators=cars, strategy=STRATEGIES[strategy]())
        sim.schedule_all(arrivals)
        sim.run_until_idle()  # 마지막 승객까지 내려야 끝난다
        stats = sim.stats()
        result.passengers += sim.passengers
        result.floors_traveled += stats.floors_traveled
        result.stops += stats.stops
        result.runs += 1
    return result


def row(strategy: str, r: Result) -> str:
    """시드 여러 개의 승객을 한데 모아 백분위수를 계산한다 (시드별 p95의 평균보다 안정적)."""
    waits = [p.wait_ms for p in r.passengers]
    journeys = [p.journey_ms for p in r.passengers]
    mean_wait = sum(waits) / len(waits) / 1000
    p95_wait = percentile(waits, 95) / 1000
    mean_journey = sum(journeys) / len(journeys) / 1000
    p95_journey = percentile(journeys, 95) / 1000
    return (
        f"| {strategy} | {mean_wait:.1f} | {p95_wait:.1f} | {mean_journey:.1f} | {p95_journey:.1f}"
        f" | {r.floors_traveled / r.runs:.0f} | {r.stops / r.runs:.0f} |"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="배차 전략 비교 실험")
    parser.add_argument("--floors", type=int, default=15)
    parser.add_argument("--cars", type=int, default=4)
    parser.add_argument("--minutes", type=int, default=30, help="승객이 도착하는 시간(분)")
    parser.add_argument("--seeds", type=int, default=5)
    parser.add_argument("--rates", type=float, nargs="+", default=[10, 20, 30], help="분당 승객")
    parser.add_argument("--output", help="결과를 UTF-8 Markdown 파일로 저장 (없으면 화면 출력)")
    args = parser.parse_args()

    seeds = list(range(args.seeds))
    started = time.perf_counter()
    lines = [
        f"조건: {args.floors}층, {args.cars}대, 정원 12명, 승객 도착 {args.minutes}분, "
        f"시드 {seeds}",
        "",
    ]
    for pattern in PATTERNS:
        for rate in args.rates:
            lines += [
                f"#### {pattern}, 분당 {rate:g}명",
                "",
                "| 전략 | 평균 대기(초) | p95 대기(초) | 평균 이동(초) | p95 이동(초)"
                " | 운행 층수/회 | 정지 횟수/회 |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
            for strategy in STRATEGIES:
                result = run(strategy, pattern, rate, seeds, args.floors, args.cars, args.minutes)
                lines.append(row(strategy, result))
            lines.append("")
    text = "\n".join(lines)
    if args.output:
        # 화면 출력을 파일로 넘기면(>) Windows에서는 cp949로 저장돼 한글이 깨진다.
        # 그래서 파일은 직접 UTF-8로 쓴다 (docs/troubleshooting.md).
        with open(args.output, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
    else:
        print(text)
    print(f"(실행 시간 {time.perf_counter() - started:.1f}초)")


if __name__ == "__main__":
    main()
