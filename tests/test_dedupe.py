import time

from slackbot.dedupe import DeliveryDedupe


def test_consume_returns_true_once_then_false() -> None:
    d = DeliveryDedupe()
    d.mark("s1", "hi")
    assert d.consume("s1", "hi") is True
    assert d.consume("s1", "hi") is False


def test_consume_no_match() -> None:
    d = DeliveryDedupe()
    d.mark("s1", "hi")
    assert d.consume("s1", "bye") is False
    assert d.consume("s2", "hi") is False
    # mark still present for the right key
    assert d.consume("s1", "hi") is True


def test_ttl_expires_marks() -> None:
    d = DeliveryDedupe(ttl_seconds=0.05)
    d.mark("s1", "hi")
    time.sleep(0.1)
    assert d.consume("s1", "hi") is False
