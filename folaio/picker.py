"""Opens the computer's own "choose a folder" window (Finder / Explorer)."""
from __future__ import annotations

import shutil
import subprocess
import sys

PROMPT = "Choose where Folaio keeps its brains"


def pick_folder() -> str | None:
    """Returns the chosen folder, None if the user cancelled.
    Raises NotImplementedError where no folder window is available."""
    if sys.platform == "darwin":
        script = (f'tell application (path to frontmost application as text) to '
                  f'POSIX path of (choose folder with prompt "{PROMPT}")')
        cmd = ["osascript", "-e", script]
    elif sys.platform == "win32":
        ps = ("Add-Type -AssemblyName System.Windows.Forms;"
              "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
              f"$d.Description = '{PROMPT}'; $d.ShowNewFolderButton = $true;"
              "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }")
        cmd = ["powershell", "-NoProfile", "-STA", "-Command", ps]
    elif shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", "--directory", f"--title={PROMPT}"]
    else:
        raise NotImplementedError
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    path = result.stdout.strip()
    return path if result.returncode == 0 and path else None
