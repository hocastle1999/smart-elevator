"""접속자별 호출 빈도 제한 (슬라이딩 윈도 로그 방식).

공유 시뮬레이션이라 한 사람이 버튼을 마구 누르면 모두의 화면이 어지러워진다. 그래서
"최근 window초 동안 limit번"까지만 받는다.

- 고정 윈도(10초 단위로 횟수 초기화): 가장 단순하지만 경계 직전·직후에 몰아 보내면 2배까지 통과한다.
- 토큰 버킷: 메모리가 키당 숫자 2개로 가장 적고 순간 몰림(burst)을 허용한다.
- 슬라이딩 윈도 로그(채택): 요청 시각을 최대 limit개 저장해 정확히 센다. limit이 작아
  메모리 부담이 없고, 동작을 설명하기 가장 쉽다.

이 제한은 서버 메모리에만 있으므로 서버가 여러 대면 각자 센다(지금은 한 대).
"""

import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        limit: int,
        window_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        # 벽시계(time.time)가 아니라 monotonic: 시스템 시각이 바뀌어도 거꾸로 가지 않는다.
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str) -> float:
        """허용하면 0을, 거절하면 다시 시도할 수 있을 때까지 남은 초를 돌려준다."""
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self.window_s:
            hits.popleft()
        if len(hits) >= self.limit:
            return hits[0] + self.window_s - now
        hits.append(now)
        self._forget_idle_keys(now)
        return 0.0

    def _forget_idle_keys(self, now: float) -> None:
        # 한동안 요청이 없던 접속자의 기록을 지워 메모리가 계속 늘지 않게 한다.
        if len(self._hits) < 1000:
            return
        cutoff = now - self.window_s
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[key]


def client_ip(peer: str | None, forwarded_for: str | None, trusted_hops: int) -> str:
    """빈도 제한에 쓸 접속자 IP.

    배포 플랫폼(Render 등)에서는 요청이 프록시를 거쳐 오므로 접속한 상대(peer)는 프록시다.
    그대로 쓰면 모든 방문자가 한도 하나를 같이 쓴다. 프록시는 실제 접속자 IP를
    X-Forwarded-For 헤더의 "오른쪽 끝"에 덧붙인다.

    헤더의 왼쪽 값은 믿으면 안 된다: 방문자가 X-Forwarded-For: 1.2.3.4 를 직접 넣어 보내면
    프록시는 그 뒤에 실제 IP를 붙일 뿐이라, 맨 왼쪽 값을 쓰면 요청마다 IP를 바꿔 제한을 피할 수
    있다. 그래서 신뢰하는 프록시 수(trusted_hops)만큼 오른쪽에서 센 값을 쓴다.
    (uvicorn --proxy-headers --forwarded-allow-ips="*"도 이 위조를 막지 못해 쓰지 않는다.)
    """
    fallback = peer or "unknown"
    if trusted_hops <= 0 or not forwarded_for:
        return fallback
    hops = [part.strip() for part in forwarded_for.split(",") if part.strip()]
    if len(hops) < trusted_hops:
        return fallback
    return hops[-trusted_hops]
