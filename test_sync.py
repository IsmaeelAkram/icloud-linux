import subprocess
import unittest
from unittest.mock import Mock, patch

from sync import get_driver_pid


class DriverPidTests(unittest.TestCase):
    def test_running_user_service_takes_precedence(self):
        with patch(
            "sync.subprocess.run", return_value=Mock(stdout=" 123\n")
        ) as run:
            self.assertEqual(get_driver_pid(), 123)

        run.assert_called_once_with(
            ["systemctl", "--user", "show", "icloud.service", "--property=MainPID", "--value"],
            capture_output=True,
            text=True,
            check=True,
        )

    def test_falls_back_to_running_system_service(self):
        for user_result in (
            Mock(stdout="0\n"),
            Mock(stdout="-1\n"),
            Mock(stdout=""),
            Mock(stdout="not-a-pid"),
            subprocess.CalledProcessError(1, ["systemctl", "--user"]),
        ):
            with self.subTest(user_result=user_result):
                with patch(
                    "sync.subprocess.run",
                    side_effect=[user_result, Mock(stdout="456\n")],
                ) as run:
                    self.assertEqual(get_driver_pid(), 456)

                self.assertEqual(run.call_count, 2)
                self.assertIn("--user", run.call_args_list[0].args[0])
                self.assertEqual(
                    run.call_args_list[1].args[0],
                    ["systemctl", "show", "icloud.service", "--property=MainPID", "--value"],
                )

    def test_returns_none_when_neither_service_has_a_valid_pid(self):
        for system_result in (
            Mock(stdout="0\n"),
            Mock(stdout="-1\n"),
            Mock(stdout=""),
            Mock(stdout="not-a-pid"),
            subprocess.CalledProcessError(1, ["systemctl"]),
        ):
            with self.subTest(system_result=system_result):
                with patch(
                    "sync.subprocess.run",
                    side_effect=[Mock(stdout="0\n"), system_result],
                ) as run:
                    self.assertIsNone(get_driver_pid())

                self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
