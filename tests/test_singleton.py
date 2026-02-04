import pytest
import threading
from src.overlayr.utils.singleton import SingletonMeta

class SingletonClass(metaclass=SingletonMeta):
    def __init__(self):
        self.value = 0

def test_singleton_instance():
    s1 = SingletonClass()
    s2 = SingletonClass()
    assert s1 is s2

def test_singleton_threading():
    instances = []
    def create_instance():
        instances.append(SingletonClass())
    
    threads = [threading.Thread(target=create_instance) for _ in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()
    
    assert all(i is instances[0] for i in instances)
