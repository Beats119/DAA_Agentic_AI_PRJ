"""Capability probe: honest booleans, real compiler, no assumptions."""

from autograder.app.sandbox import probe_capabilities
from autograder.app.sandbox.landlock import landlock_abi_version


def test_probe_reports_honest_types():
    caps = probe_capabilities()
    assert isinstance(caps.user_namespaces, bool)
    assert isinstance(caps.network_namespace, bool)
    assert isinstance(caps.seccomp, bool)
    assert isinstance(caps.cgroup_v2, bool)
    assert isinstance(caps.resource_limits, bool)
    assert caps.landlock_abi is None or isinstance(caps.landlock_abi, int)
    # This build machine must be able to sandbox at all.
    assert caps.resource_limits is True
    assert caps.compiler is not None and caps.compiler.endswith(("gcc", "clang"))


def test_probe_is_repeatable_and_agrees_with_landlock_module():
    a = probe_capabilities()
    b = probe_capabilities()
    assert a.model_dump() == b.model_dump()
    assert a.landlock_abi == landlock_abi_version()
