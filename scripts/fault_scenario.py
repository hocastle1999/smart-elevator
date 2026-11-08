"""고장 시나리오 실험: 이상 감지(자동 격리)가 승객 대기 시간을 얼마나 지키는가.

같은 승객 목록에 같은 시각 같은 고장을 넣고, 감지기를 켰을 때와 껐을 때를 비교한다.
엔진이 결정적이라 차이는 오직 감지 여부에서 나온다.

실행 (프로젝트 루트):
    .\\.venv\\Scripts\\python.exe -m scripts.fault_scenario --output fault.md
"""

import argparse
import time

from app.simulation import STRATEGIES, Building, Simulation
from app.simulation.monitor import Monitor, ServiceKind
from app.simulation.passengers import percentile
from app.simulation.traffic import PATTERNS, poisson_passengers
from app.simulation.types import Fault

MINUTE = 60_000


def run_once(fault: Fault | None, monitor: bool, seed: int, args: argparse.Namespace):
    building = Building(num_floors=args.floors)
    sim = Simulation(building, num_elevators=args.cars, strategy=STRATEGIES[args.strategy]())
    if monitor:
        Monitor(sim)
    sim.schedule_all(
        poisson_passengers(building, PATTERNS[args.pattern], args.rate, args.minutes * MINUTE, seed)
    )
    sim.run_until(args.fault_at * MINUTE, dt_ms=MINUTE)
    if fault is not None:
        sim.inject_fault(0, fault)
    sim.run_until(args.repair_at * MINUTE, dt_ms=MINUTE)
    if fault is not None:
        sim.repair(0)
        sim.return_to_service(0)  # 감지기를 껐으면 운행 중이므로 아무 일도 하지 않는다
    sim.run_until_idle()
    detected = [
        e.time_ms - args.fault_at * MINUTE for e in sim.service_log if e.kind is ServiceKind.ALERT
    ]
    return sim.passengers, (detected[0] if detected else None)


def row(label: str, results: list) -> str:
    waits = [p.wait_ms for passengers, _ in results for p in passengers]
    journeys = [p.journey_ms for passengers, _ in results for p in passengers]
    latencies = [d for _, d in results if d is not None]
    detect = f"{sum(latencies) / len(latencies) / 1000:.1f}" if latencies else "-"
    over3 = sum(w > 3 * MINUTE for w in waits)
    return (
        f"| {label} | {sum(waits) / len(waits) / 1000:.1f} | {percentile(waits, 95) / 1000:.1f}"
        f" | {max(waits) / 1000:.1f} | {over3} | {sum(journeys) / len(journeys) / 1000:.1f}"
        f" | {detect} |"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="고장 시나리오 실험")
    parser.add_argument("--floors", type=int, default=15)
    parser.add_argument("--cars", type=int, default=4)
    parser.add_argument("--strategy", default="eta", choices=sorted(STRATEGIES))
    parser.add_argument("--pattern", default="lunch", choices=sorted(PATTERNS))
    parser.add_argument("--rate", type=float, default=20)
    parser.add_argument("--minutes", type=int, default=30)
    parser.add_argument("--fault-at", type=int, default=5, help="고장 시각(분)")
    parser.add_argument("--repair-at", type=int, default=20, help="수리 시각(분)")
    parser.add_argument("--seeds", type=int, default=5)
    parser.add_argument("--output", help="결과를 UTF-8 Markdown 파일로 저장")
    args = parser.parse_args()

    started = time.perf_counter()
    seeds = range(args.seeds)
    lines = [
        f"조건: {args.floors}층 {args.cars}대, {args.strategy}, {args.pattern} 분당 {args.rate:g}명"
        f" {args.minutes}분, {args.fault_at}분에 0번 카 고장 -> {args.repair_at}분에 수리,"
        f" 시드 {list(seeds)}",
        "",
        "| 경우 | 평균 대기(초) | p95 대기(초) | 최대 대기(초) | 3분 넘게 기다린 승객"
        " | 평균 이동(초) | 감지까지(초) |",
        "|---|---:|---:|---:|---:|---:|---:|",
        row("고장 없음", [run_once(None, False, s, args) for s in seeds]),
    ]
    for fault in Fault:
        for monitor in (False, True):
            label = f"{fault.value}, 감지 {'켬' if monitor else '끔'}"
            lines.append(row(label, [run_once(fault, monitor, s, args) for s in seeds]))
    text = "\n".join(lines)
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
    else:
        print(text)
    print(f"(실행 시간 {time.perf_counter() - started:.1f}초)")


if __name__ == "__main__":
    main()
