"""Tests for SingletonMeta — thread safety and identity."""
import pytest
import threading
from overlayr.utils.singleton import SingletonMeta


class _TestSingleton(metaclass=SingletonMeta):
    """Test class for singleton behavior."""
    def __init__(self):
        self.value = 0


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset singleton instances between tests."""
    _TestSingleton._instances = {}
    yield
    _TestSingleton._instances = {}


class TestSingletonIdentity:
    def test_same_instance(self):
        s1 = _TestSingleton()
        s2 = _TestSingleton()
        assert s1 is s2

    def test_shared_state(self):
        s1 = _TestSingleton()
        s1.value = 42
        s2 = _TestSingleton()
        assert s2.value == 42


class TestSingletonThreadSafety:
    def test_concurrent_creation(self):
        instances = []

        def create():
            instances.append(_TestSingleton())

        threads = [threading.Thread(target=create) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(i is instances[0] for i in instances)
