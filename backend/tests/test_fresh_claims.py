"""Reading a PolitiFact ruling from its article for the recent-claims test."""
import pytest

from eval.fresh_claims import ruling


@pytest.mark.parametrize("text,label", [
    ("Our ruling ... We rate it Mostly False.", "Conflicting"),
    ("We rate this claim Pants on Fire!", "False"),
    ("We rate it Half True.", "Conflicting"),
    ("We rate the statement Mostly True.", "True"),
    ("Earlier we rate it False. Updated: We rate it True.", "True"),
    ("No ruling here.", ""),
])
def test_ruling(text, label):
    assert ruling(text) == label
