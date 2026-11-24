import unittest
from unittest.mock import Mock, patch

from src.agent_sessions import observe


class ObserveWindowTests(unittest.TestCase):
    def test_late_redraw_within_window_is_confirmed(self):
        # A pane that redraws after about 1.5 s must not be reported as unconfirmed.
        slept = []
        with patch("time.sleep", side_effect=slept.append):
            self.assertTrue(observe.observed(lambda: sum(slept) >= 1.5))

    def test_temporary_read_error_does_not_end_confirmation(self):
        check = Mock(side_effect=[observe.SourceError("busy"), False, True])
        with patch("time.sleep"):
            self.assertTrue(observe.observed(check))
        self.assertEqual(check.call_count, 3)

    def test_slow_read_uses_poll_budget(self):
        check = Mock(side_effect=observe.SourceError("timeout"))
        with patch("time.monotonic", side_effect=[0, 0, 5]):
            with self.assertRaisesRegex(observe.SourceError, "timeout"):
                observe.observed(check)
        check.assert_called_once()

    def test_window_is_bounded(self):
        slept = []
        with patch("time.sleep", side_effect=slept.append):
            self.assertFalse(observe.observed(lambda: False))
        self.assertLessEqual(sum(slept), 5)


if __name__ == "__main__":
    unittest.main()
