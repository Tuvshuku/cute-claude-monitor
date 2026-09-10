import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import native_app


class NativeLauncherTests(unittest.TestCase):
    def test_widget_uses_system_powershell_and_passes_paths_as_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            powershell = (
                root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            )
            powershell.parent.mkdir(parents=True)
            powershell.write_bytes(b"")
            widget = root / "widget.ps1"
            widget.write_text("# test")
            data_path = root / "profile with spaces" / "usage.json"

            with patch.dict(os.environ, {"SystemRoot": str(root)}), patch.object(
                native_app, "bundled_asset", return_value=widget
            ), patch.object(native_app.subprocess, "Popen") as popen:
                native_app.launch_widget(data_path)

            command = popen.call_args.args[0]
            self.assertEqual(command[0], str(powershell))
            self.assertEqual(command[command.index("-DataPath") + 1], str(data_path))
            self.assertEqual(command[command.index("-File") + 1], str(widget))
            self.assertIn("-NativeMode", command)


if __name__ == "__main__":
    unittest.main()
