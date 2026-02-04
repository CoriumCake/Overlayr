from threading import RLock
from typing import Any, Generic, TypeVar

T = TypeVar("T")

class SingletonMeta(type):
    """
    Thread-safe implementation of Singleton Metaclass.
    """
    _instances: dict[type, Any] = {}
    _lock: RLock = RLock()

    def __call__(cls, *args: Any, **kwargs: Any) -> Any:
        with cls._lock:
            if cls not in cls._instances:
                instance = super().__call__(*args, **kwargs)
                cls._instances[cls] = instance
        return cls._instances[cls]
