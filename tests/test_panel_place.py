# pyright: reportMissingImports=false
"""emu/dtpanel.py: which monitor the window opens on (xrandr --listmonitors)."""
import subprocess
import unittest
from unittest import mock

from emu import dtpanel

# Three monitors: the primary (HDMI-A-0) in the middle, lower than the
# right-hand portrait one and higher than the small left-hand one.
LISTING = """Monitors: 3
 0: +*HDMI-A-0 2560/597x1440/336+1360+217  HDMI-A-0
 1: +DisplayPort-0 1360/708x768/398+0+581  DisplayPort-0
 2: +DisplayPort-2 1080/509x1920/286+3920+0  DisplayPort-2
"""


def _xrandr(stdout):
    return mock.patch.object(dtpanel.subprocess, 'run', return_value=(
        subprocess.CompletedProcess([], 0, stdout=stdout)))


class HomeMonitor(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(dtpanel._monitors(LISTING), [
            (True, 1360, 217, 2560, 1440),
            (False, 0, 581, 1360, 768),
            (False, 3920, 0, 1080, 1920)])

    def test_primary_wins_over_pointer(self):
        with _xrandr(LISTING):
            self.assertEqual(dtpanel._home_monitor((4000, 100)),
                             (1360, 217, 2560, 1440))

    def test_no_primary_uses_pointer(self):
        with _xrandr(LISTING.replace('+*', '+')):
            self.assertEqual(dtpanel._home_monitor((4000, 100)),
                             (3920, 0, 1080, 1920))
            self.assertIsNone(dtpanel._home_monitor((10, 10)))

    def test_no_xrandr(self):
        with mock.patch.object(dtpanel.subprocess, 'run',
                               side_effect=FileNotFoundError):
            self.assertIsNone(dtpanel._home_monitor((0, 0)))


if __name__ == '__main__':
    unittest.main()
