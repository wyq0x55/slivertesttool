import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from silver_cli import credentials


def test_private_write_persists_credentials_and_cleans_temporary_file(tmp_path, monkeypatch):
    target = tmp_path / "saved.json"
    monkeypatch.setenv("SILVER_CLI_CONFIG", str(target))
    credentials.save("http://server/", {"token": "secret", "allow_http": True})
    assert credentials.saved("http://server")[1]["token"] == "secret"
    assert list(tmp_path.iterdir()) == [target]
    if os.name == "nt":
        import subprocess

        script = (
            'Import-Module "$PSHOME/Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1";'
            "$acl=Get-Acl -LiteralPath $env:SILVER_CLI_CREDENTIAL_PATH;"
            "$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value;"
            "if(-not $acl.AreAccessRulesProtected){exit 1};"
            "foreach($ace in $acl.Access){"
            "if($ace.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -ne $sid)"
            "{exit 2}}"
        )
        environment = os.environ.copy()
        environment["SILVER_CLI_CREDENTIAL_PATH"] = str(target)
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
                       env=environment, check=True, capture_output=True, timeout=15,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        assert target.stat().st_mode & 0o777 == 0o600


def test_failure_to_protect_never_writes_a_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("SILVER_CLI_CONFIG", str(tmp_path / "saved.json"))

    def fail(_target):
        raise OSError("cannot protect")

    monkeypatch.setattr(credentials, "_protect", fail)
    with pytest.raises(OSError, match="cannot protect"):
        credentials.save("http://server", {"token": "secret"})
    assert list(tmp_path.iterdir()) == []


def test_http_permission_is_bound_to_saved_server(tmp_path, monkeypatch):
    from silver_cli.runner import _client

    monkeypatch.setenv("SILVER_CLI_CONFIG", str(tmp_path / "saved.json"))
    for key in ("SILVER_CLI_USERNAME", "SILVER_CLI_PASSWORD", "SILVER_CLI_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    credentials.save("http://first-server", {"token": "secret", "allow_http": True})
    args = SimpleNamespace(url="http://first-server", timeout=1, allow_http=False)
    _client(args)
    assert args.allow_http is True
    args = SimpleNamespace(url="https://other-server", timeout=1, allow_http=False)
    _client(args)
    assert args.allow_http is False
