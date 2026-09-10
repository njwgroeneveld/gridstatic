from unittest.mock import patch
from src.alert_throttle import OutsideGridThrottle


def test_first_breach_always_alerts():
    t = OutsideGridThrottle()
    assert t.should_alert("top") is True


def test_repeated_breach_within_cooldown_does_not_alert():
    t = OutsideGridThrottle()
    assert t.should_alert("top") is True
    assert t.should_alert("top") is False
    assert t.should_alert("top") is False


def test_repeated_breach_after_cooldown_alerts_again():
    t = OutsideGridThrottle()
    with patch("src.alert_throttle.time.time", return_value=1000.0):
        assert t.should_alert("top") is True
    with patch("src.alert_throttle.time.time", return_value=1000.0 + 3600):
        assert t.should_alert("top") is True


def test_clear_then_new_breach_alerts_immediately():
    t = OutsideGridThrottle()
    with patch("src.alert_throttle.time.time", return_value=1000.0):
        assert t.should_alert("top") is True
    with patch("src.alert_throttle.time.time", return_value=1000.5):
        assert t.should_alert("top") is False  # still within cooldown, still active
    t.clear("top")
    with patch("src.alert_throttle.time.time", return_value=1001.0):
        assert t.should_alert("top") is True  # re-entered and breached again -> immediate alert


def test_top_and_bottom_are_independent():
    t = OutsideGridThrottle()
    assert t.should_alert("top") is True
    assert t.should_alert("bottom") is True
    assert t.should_alert("top") is False
    assert t.should_alert("bottom") is False
