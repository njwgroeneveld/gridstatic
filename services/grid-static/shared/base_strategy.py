from abc import ABC, abstractmethod


class BaseStrategy(ABC):
    @abstractmethod
    async def initialize(self) -> None: ...

    @abstractmethod
    async def recover_state(self) -> None: ...

    @abstractmethod
    async def run_fill_loop(self) -> None: ...

    @abstractmethod
    async def run_health_loop(self) -> None: ...
