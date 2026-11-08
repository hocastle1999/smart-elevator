"""건물 정보. 지금은 층 수만 있다."""

from dataclasses import dataclass

from app.simulation.types import Direction


@dataclass(frozen=True)
class Building:
    """층 번호는 사람이 쓰는 대로 1층부터 num_floors층까지다 (지하층은 다루지 않는다)."""

    num_floors: int

    def __post_init__(self) -> None:
        if self.num_floors < 2:
            raise ValueError("건물은 2개 층 이상이어야 합니다.")

    @property
    def bottom(self) -> int:
        return 1

    @property
    def top(self) -> int:
        return self.num_floors

    def validate_floor(self, floor: int) -> None:
        if not self.bottom <= floor <= self.top:
            raise ValueError(f"{floor}층은 건물 범위({self.bottom}~{self.top}) 밖입니다.")

    def validate_hall_call(self, floor: int, direction: Direction) -> None:
        self.validate_floor(floor)
        if direction is Direction.NONE:
            raise ValueError("홀 호출은 UP 또는 DOWN이어야 합니다.")
        # 맨 위층에는 올라감 버튼이, 맨 아래층에는 내려감 버튼이 없다.
        if floor == self.top and direction is Direction.UP:
            raise ValueError("맨 위층에서는 올라가는 호출을 할 수 없습니다.")
        if floor == self.bottom and direction is Direction.DOWN:
            raise ValueError("맨 아래층에서는 내려가는 호출을 할 수 없습니다.")
