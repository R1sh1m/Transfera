"""
Tests for the WSL service-failure signature classifier (classify_wsl_failure).

These tests are pure-Python; no subprocess or WSL installation needed.
"""

from __future__ import annotations

from backend.wsl_orchestrator import classify_wsl_failure

# ---------------------------------------------------------------------------
# Service-broken signatures
# ---------------------------------------------------------------------------

class TestServiceBroken:
    """Return code or output that conclusively indicates a broken WSL service."""

    def test_rc_4294967295(self):
        """unsigned 0xFFFFFFFF — the canonical E_UNEXPECTED return code."""
        assert classify_wsl_failure(4294967295, "", "") == "service_broken"

    def test_rc_minus_one(self):
        """signed -1 alias for 0xFFFFFFFF on some Python/OS combos."""
        assert classify_wsl_failure(-1, "", "") == "service_broken"

    def test_e_unexpected_in_stderr(self):
        assert classify_wsl_failure(
            1, "", "Error: 0x8000ffff (E_UNEXPECTED)"
        ) == "service_broken"

    def test_catastrophic_failure_in_stdout(self):
        assert classify_wsl_failure(
            1, "Catastrophic failure", ""
        ) == "service_broken"

    def test_catastrophic_failure_mixed_case(self):
        assert classify_wsl_failure(
            1, "", "A catastrophic failure occurred in the WSL runtime."
        ) == "service_broken"

    def test_wsl_service_subsystem_error(self):
        assert classify_wsl_failure(
            1, "", "Error code: Wsl/Service/0x80070003"
        ) == "service_broken"

    def test_lxss_in_output(self):
        assert classify_wsl_failure(
            1, "", "LxssManager: failed to start"
        ) == "service_broken"

    def test_0x8000ffff_hex_form(self):
        assert classify_wsl_failure(
            1, "", "hr=0x8000ffff"
        ) == "service_broken"

    def test_failed_to_attach_disk(self):
        assert classify_wsl_failure(
            1, "", "Failed to attach disk. The kernel module is not loaded."
        ) == "service_broken"

    def test_element_not_found(self):
        assert classify_wsl_failure(
            1, "", "Error: 0x80070490 Element not found."
        ) == "service_broken"

    def test_4294967295_string_in_output(self):
        """Some WSL builds print 'rc=4294967295' in the error text."""
        assert classify_wsl_failure(
            1, "", "wsl.exe returned 4294967295"
        ) == "service_broken"


# ---------------------------------------------------------------------------
# Not-installed signatures
# ---------------------------------------------------------------------------

class TestNotInstalled:
    """Errors that mean WSL or the distro is simply absent."""

    def test_not_installed_text(self):
        assert classify_wsl_failure(
            1, "", "WSL is not installed. Please install WSL first."
        ) == "not_installed"

    def test_no_distribution(self):
        assert classify_wsl_failure(
            1, "No distribution registered.", ""
        ) == "not_installed"

    def test_distro_not_found_error_code(self):
        assert classify_wsl_failure(
            1, "", "Error: 0x80370114 WSL_E_DISTRO_NOT_FOUND"
        ) == "not_installed"

    def test_please_install_a_distribution(self):
        assert classify_wsl_failure(
            1, "Please install a distribution.", ""
        ) == "not_installed"

    def test_install_ubuntu_text(self):
        assert classify_wsl_failure(
            1, "", "Please install ubuntu from the store."
        ) == "not_installed"


# ---------------------------------------------------------------------------
# Transient / unknown errors
# ---------------------------------------------------------------------------

class TestTransient:
    """Non-zero rc with no recognisable signature -> transient."""

    def test_generic_nonzero(self):
        assert classify_wsl_failure(1, "", "Unknown error") == "transient"

    def test_timeout_like_error(self):
        assert classify_wsl_failure(
            2, "", "Operation timed out"
        ) == "transient"

    def test_empty_output_nonzero(self):
        assert classify_wsl_failure(255, "", "") == "transient"


# ---------------------------------------------------------------------------
# OK
# ---------------------------------------------------------------------------

class TestOk:
    def test_rc_zero_is_always_ok(self):
        assert classify_wsl_failure(0, "", "") == "ok"

    def test_rc_zero_ignores_suspicious_output(self):
        """rc=0 wins even if output contains service-broken text (shouldn't
        happen in practice, but classifier should be robust to it)."""
        assert classify_wsl_failure(0, "catastrophic failure", "") == "ok"
