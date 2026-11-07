"""Guard the D5-05 Windows entrypoint against PowerShell 5.1 encoding regressions."""

from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "infra" / "start.ps1"


class WindowsStartTests(unittest.TestCase):
    def test_start_script_is_ascii_for_windows_powershell_51(self):
        # Windows PowerShell 5.1 decodes UTF-8 without BOM using its ANSI code page.
        # An ASCII entrypoint stays unambiguous on both old and new PowerShell.
        SCRIPT.read_bytes().decode("ascii")

    def test_start_script_allows_the_approved_online_route(self):
        source = SCRIPT.read_text(encoding="ascii")
        self.assertNotIn("if ($Mode -eq 'online')", source)
        self.assertIn("--mode $Mode", source)

    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell unavailable")
    def test_windows_powershell_parser_accepts_start_script(self):
        path = str(SCRIPT).replace("'", "''")
        command = (
            "$tokens=$null; $errors=$null; "
            f"[System.Management.Automation.Language.Parser]::ParseFile('{path}',"
            "[ref]$tokens,[ref]$errors) | Out-Null; "
            "if ($errors.Count) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell unavailable")
    def test_python_version_probe_works_in_windows_powershell(self):
        source = SCRIPT.read_text(encoding="ascii")
        probe = re.search(r"(?m)^\$version = & \$pythonCommand @pythonArgs -c '[^']+'", source)
        self.assertIsNotNone(probe)
        python = sys.executable.replace("'", "''")
        expected_version = str(sys.version_info.major * 100 + sys.version_info.minor)
        command = (
            f"$pythonCommand='{python}'; $pythonArgs=@(); {probe.group(0)}; "
            f"if ($LASTEXITCODE -ne 0 -or $version -ne '{expected_version}') {{ exit 1 }}"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
