from collections.abc import Callable

from indexer.parsing.contracts import Parser, ParserConfig


class ParserRegistry:
    def __init__(self):
        self._factories: dict[str, Callable[[ParserConfig], Parser]] = {}

    def register(self, name: str, factory: Callable[[ParserConfig], Parser]):
        if not name or name in self._factories:
            raise ValueError("파서 이름이 비어 있거나 중복됩니다")
        self._factories[name] = factory

    def create(self, config: ParserConfig) -> Parser:
        if config.provider not in self._factories:
            raise ValueError(f"등록되지 않은 파서: {config.provider}")
        return self._factories[config.provider](config)


def _upstage(config, client, remote_timeout, job_timeout):
    from indexer.parsing.upstage_adapter import UpstageParser

    return UpstageParser(config, client, remote_timeout=remote_timeout, job_timeout=job_timeout)


def _paddle_vl(config, client, remote_timeout, job_timeout):
    from indexer.parsing.paddle_vl_adapter import PaddleVLParser

    return PaddleVLParser(config, client)


PROVIDERS = {"upstage": _upstage, "paddle_vl": _paddle_vl}


def provider_names():
    return frozenset(PROVIDERS)


def build_registry(client, *, remote_timeout, job_timeout):
    registry = ParserRegistry()
    for name, factory in PROVIDERS.items():
        registry.register(name, lambda config, factory=factory: factory(config, client, remote_timeout, job_timeout))
    return registry
