import pytest

from app.simulation import Building


@pytest.fixture
def building() -> Building:
    return Building(num_floors=10)
