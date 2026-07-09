"""Security-regression guard for the deploy-gating starlette CVE remediation.

`starlette 0.41.3` carried four HIGH / CVSS-7.5 CVEs (GHSA-7f5h-v6xp-fcq8,
GHSA-82w8-qh3p-5jfq, GHSA-wqp7-x3pw-xc5r, PYSEC-2026-249) plus six MODERATE/LOW.
All ten are cleared in starlette 1.3.1. The deployment gate blocks on
`osv_max_cvss >= 7.0`, so a silent downgrade of starlette back into the
vulnerable range must be caught here rather than at the deploy gate.

fastapi pins starlette, so fastapi and starlette move together: fastapi 0.133.0
is the first release to lift the `<1.0.0` cap and admit starlette 1.3.1.
"""

from importlib.metadata import version

from packaging.version import Version

# Floor that clears every known starlette CVE from the 2026-07 OSV scan.
STARLETTE_SAFE_FLOOR = Version("1.3.1")
# fastapi floor that admits (pins-compatible with) the safe starlette.
FASTAPI_SAFE_FLOOR = Version("0.133.0")


def test_starlette_is_at_or_above_cve_clearing_floor():
    """Installed starlette must clear the HIGH-severity CVEs (>= 1.3.1)."""
    installed = Version(version("starlette"))
    assert installed >= STARLETTE_SAFE_FLOOR, (
        f"starlette {installed} is below the CVE-clearing floor "
        f"{STARLETTE_SAFE_FLOOR}; it reintroduces the deploy-gating CVSS-7.5 CVEs."
    )


def test_fastapi_admits_the_safe_starlette():
    """fastapi must be new enough to pin-permit the safe starlette (>= 0.133.0)."""
    installed = Version(version("fastapi"))
    assert installed >= FASTAPI_SAFE_FLOOR, (
        f"fastapi {installed} caps starlette below the safe floor; "
        f"bump fastapi to >= {FASTAPI_SAFE_FLOOR} alongside starlette."
    )
