from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1] / "scripts" / "install-api-server-task.ps1"
INTERACTIVE_INSTALLER = INSTALLER.with_suffix(".cmd")


def test_api_server_installer_defaults_to_background_startup_task() -> None:
    """A reboot must restore the loopback API without an interactive logon."""
    script = INSTALLER.read_text(encoding="utf-8")

    assert "New-ScheduledTaskTrigger -AtStartup" in script
    assert "Get-Credential" in script
    assert "-Password $RunAsCredential.GetNetworkCredential().Password" in script
    assert "[switch]$AtLogOn" in script


def test_double_click_installer_keeps_the_window_open() -> None:
    """Credential or registration errors must remain visible to the owner."""
    script = INTERACTIVE_INSTALLER.read_text(encoding="utf-8")

    assert "-NoExit" in script
    assert "install-api-server-task.ps1" in script
