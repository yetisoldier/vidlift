"""Constructor compatibility: preserve registry identity and recover once."""
import pytest

from vidlift.mcp_bridge.registry import Registry
from vidlift.mcp_bridge.server import McpServer, build_services
from vidlift.mcp_bridge.tools import BridgeServices


@pytest.mark.parametrize("case", ["default", "injected-none", "injected-real", "explicit-none", "explicit-override", "falsey-injected", "falsey-explicit"])
def test_registry_constructor_precedence(tmp_path, monkeypatch, case):
    class FalseyRegistry(Registry):
        def __bool__(self):
            return False

    state = tmp_path / "state"
    services = None if case == "default" else BridgeServices(registry=None, state_dir=state)
    injected = None
    explicit = None
    if case in {"injected-real", "explicit-override", "falsey-injected"}:
        cls = FalseyRegistry if case == "falsey-injected" else Registry
        injected = cls(tmp_path / "injected")
        assert services is not None
        services.registry = injected
    if case in {"explicit-none", "explicit-override", "falsey-explicit"}:
        cls = FalseyRegistry if case == "falsey-explicit" else Registry
        explicit = cls(tmp_path / "explicit")
    sweeps = []
    original = Registry.sweep_orphans

    def sweep(registry, **kwargs):
        # Other suites may still be draining executor threads. Count every
        # constructor-owned registry, not unrelated process-wide sweeps.
        if registry.path.is_relative_to(tmp_path):
            sweeps.append(registry)
        return original(registry, **kwargs)

    monkeypatch.setattr(Registry, "sweep_orphans", sweep)
    server = McpServer(state_dir=state, services=services, registry=explicit, start_workers=False)
    expected = explicit if explicit is not None else injected
    if expected is not None:
        assert server.registry is expected
    else:
        assert server.registry.path == state / "jobs.json"
    assert sweeps == [server.registry]
    assert server.services.registry is server.registry
    assert server.executor.registry is server.registry
    assert server.services.executor is server.executor
    assert server.executor._threads == []


@pytest.mark.parametrize("override", [False, True])
def test_precovered_services_skip_only_the_same_registry(tmp_path, monkeypatch, override):
    services = build_services(tmp_path / "recovered")
    replacement = Registry(tmp_path / "replacement") if override else None
    sweeps = []
    original = Registry.sweep_orphans

    def sweep(registry, **kwargs):
        if registry.path.is_relative_to(tmp_path):
            sweeps.append(registry)
        return original(registry, **kwargs)

    monkeypatch.setattr(Registry, "sweep_orphans", sweep)
    server = McpServer(state_dir=services.state_dir, services=services,
                       registry=replacement, start_workers=False)
    assert sweeps == ([replacement] if override else [])
    assert server.executor.registry is server.registry is server.services.registry
    assert server.executor._threads == []
