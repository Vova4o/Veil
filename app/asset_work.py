"""Serialize index mutations and derived-asset publication in this local process."""
from functools import wraps
from threading import RLock

asset_work_lock = RLock()


def serialized_asset_work(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with asset_work_lock:
            return function(*args, **kwargs)
    return wrapped
