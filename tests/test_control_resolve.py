"""The OSS/ECC plugin seam: control/interfaces.py, control/local.py,
control/resolve.py.

Runs WITHOUT Postgres, same discipline as test_control_plane.py -- these
exercise the resolution mechanism itself, not ControlPlane's own row-level
behavior (which stays covered there, through fakes).
"""

from __future__ import annotations

import importlib.metadata
import sys
import textwrap

import control.resolve as resolve
from control.interfaces import Authenticator
from control.keys import INGEST, READ
from control.local import LocalAuthenticator
from control.models import Principal


class TestLocalAuthenticator:
    def test_accepts_any_token_as_the_static_local_principal(self) -> None:
        auth = LocalAuthenticator()
        principal = auth.authenticate("anything, or nothing")
        assert isinstance(principal, Principal)
        assert principal.tenant == "local"
        assert principal.plan == "local"
        assert principal.can(READ)
        assert principal.can(INGEST)

    def test_accepts_a_missing_token_too(self) -> None:
        """A gateway that requires SOME token even in single-tenant mode would
        make "local" a worse experience than the multi-tenant one it stands in
        for -- there is nothing to check, so nothing should be required."""
        auth = LocalAuthenticator()
        assert auth.authenticate(None) is not None

    def test_quota_for_tenant_reports_the_plan_that_is_unconditionally_allowed(self) -> None:
        """control/quota.py's PLANS["local"] is (0, 0) == unlimited, and
        QuotaEnforcer.check() short-circuits to allow without touching Redis
        for it -- this is the whole reason QuotaEnforcer needs no local
        variant of its own."""
        auth = LocalAuthenticator()
        plan, per_minute, per_day = auth.quota_for_tenant("local")
        assert plan == "local"
        assert per_minute is None
        assert per_day is None

    def test_lifecycle_methods_are_true_no_ops(self) -> None:
        auth = LocalAuthenticator()
        auth.touch(0)  # must not raise
        auth.ping()  # must not raise
        auth.wait_ready(timeout=0.01)  # must not block or raise
        assert auth.migrate() == []

    def test_satisfies_the_authenticator_protocol(self) -> None:
        assert isinstance(LocalAuthenticator(), Authenticator)


class TestResolve:
    def setup_method(self) -> None:
        resolve.reset()

    def teardown_method(self) -> None:
        resolve.reset()

    def test_falls_back_to_local_when_no_entry_point_is_registered(self, monkeypatch) -> None:
        monkeypatch.setattr(resolve, "entry_points", lambda group: [])
        auth = resolve.authenticator()
        assert isinstance(auth, LocalAuthenticator)

    def test_prefers_a_registered_entry_point_over_the_local_default(self, monkeypatch) -> None:
        """Proves the discovery mechanism actually prefers a real
        implementation when one is registered, the same way
        mcpobs/downstream.py's entry-point discovery is exercised."""

        class FakeControlPlane:
            def authenticate(self, token: str | None) -> Principal | None:
                return None

        fake_entry_point = importlib.metadata.EntryPoint(
            name="fake-ecc",
            value=f"{__name__}:FakeControlPlane",
            group="mcpobs_control_plane",
        )
        # EntryPoint.load() resolves `value` against sys.modules by the
        # module name in `__name__` -- point it at THIS test module, which
        # defines FakeControlPlane at module scope for exactly this purpose.
        globals()["FakeControlPlane"] = FakeControlPlane
        monkeypatch.setattr(resolve, "entry_points", lambda group: [fake_entry_point])

        auth = resolve.authenticator()
        assert isinstance(auth, FakeControlPlane)

    def test_a_genuinely_installed_fake_ecc_package_wins_over_the_local_default(
        self, tmp_path, monkeypatch
    ) -> None:
        """The concrete proof the plan's Verification step 5 asks for: a
        throwaway package with a REAL entry point, discovered through the
        actual `importlib.metadata` machinery (not a monkeypatched
        `entry_points` function, unlike the test above) -- proving the
        mechanism ECC will actually depend on works, not just resolve.py's
        own call to whatever `entry_points()` happens to return.

        Built by hand (a `.dist-info` directory dropped on `sys.path`)
        because installing a real package via pip mid-test-suite is slower
        and heavier for the same proof; `importlib.metadata` does not care
        how a `.dist-info` got onto `sys.path`, only that it is there.
        """
        dist_info = tmp_path / "fake_ecc-1.0.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: fake-ecc\nVersion: 1.0.0\n"
        )
        # Named to sort ALPHABETICALLY BEFORE the real "control-plane" entry
        # that this project's own dev venv has installed (control/pyproject.
        # toml) -- resolve.py picks entry_points()'s first result by name,
        # so without this the test's outcome would depend on sys.path
        # enumeration order between the two, not on the mechanism itself.
        (dist_info / "entry_points.txt").write_text(
            textwrap.dedent(
                """\
                [mcpobs_control_plane]
                aaa-fake-ecc = fake_ecc_module:FakeECCControlPlane
                """
            )
        )
        (tmp_path / "fake_ecc_module.py").write_text(
            textwrap.dedent(
                """\
                class FakeECCControlPlane:
                    def authenticate(self, token):
                        return None
                """
            )
        )

        monkeypatch.syspath_prepend(str(tmp_path))
        importlib.invalidate_caches()
        try:
            auth = resolve.authenticator()
            assert type(auth).__name__ == "FakeECCControlPlane"
        finally:
            sys.modules.pop("fake_ecc_module", None)
            importlib.invalidate_caches()

    def test_resolution_happens_once_and_is_cached(self, monkeypatch) -> None:
        calls = []

        def fake_entry_points(group: str):
            calls.append(group)
            return []

        monkeypatch.setattr(resolve, "entry_points", fake_entry_points)
        first = resolve.authenticator()
        second = resolve.authenticator()
        assert first is second
        assert len(calls) == 1

    def test_reset_forces_re_resolution(self, monkeypatch) -> None:
        calls = []

        def fake_entry_points(group: str):
            calls.append(group)
            return []

        monkeypatch.setattr(resolve, "entry_points", fake_entry_points)
        resolve.authenticator()
        resolve.reset()
        resolve.authenticator()
        assert len(calls) == 2
