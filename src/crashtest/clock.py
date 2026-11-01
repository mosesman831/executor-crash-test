import time


class Clock:
    @staticmethod
    def monotonic_ns() -> int:
        return time.monotonic_ns()

    @staticmethod
    def wall() -> float:
        return time.time()
