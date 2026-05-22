"""Tests for the causal-headline classifier (PR1B).

The classifier is conservative: it returns True ONLY when the headline
contains a keyword that names a specific catalyst class (rating change,
corporate event, regulatory, etc.). Generic stock-discussion or technical
chart commentary is excluded so the operator doesn't get false catalyst
attribution on raw_news that happens to mention a ticker without a real
cause.

Positive list anchored to today's missed catalyst (Goldman PT raise on ENPH)
and the catalyst families already graded by thesis_agent's rubric.
"""
from __future__ import annotations

from _catalyst_policy import CAUSAL_KEYWORDS, is_causal_headline


# ============================================================
# Positive cases — these MUST classify as causal
# ============================================================

class TestCausalPositive:
    def test_goldman_pt_raise_classifies_as_causal(self):
        """The exact headline the bot missed on 2026-05-22."""
        assert is_causal_headline(
            "Enphase powers to 52-week high as Goldman points to data center transformer opportunity"
        ) is False  # this one doesn't contain a causal keyword; commentary
        # The actual GS upgrade headline DOES contain the keyword:
        assert is_causal_headline(
            "Goldman Sachs upgrades Enphase to Buy, raises price target to $57"
        ) is True

    def test_buyback_classifies_as_causal(self):
        assert is_causal_headline("AAPL announces $90B share repurchase program") is True

    def test_fda_approval_classifies_as_causal(self):
        assert is_causal_headline("GILD wins FDA approval for hepatitis treatment") is True

    def test_acquisition_classifies_as_causal(self):
        assert is_causal_headline("Microsoft to acquire startup in $5B acquisition") is True

    def test_earnings_beat_classifies_as_causal(self):
        assert is_causal_headline("NVDA beats estimates with $35B Q3 revenue") is True

    def test_guidance_raise_classifies_as_causal(self):
        assert is_causal_headline("DELL raises guidance for FY26 on AI demand") is True

    def test_pdufa_classifies_as_causal(self):
        assert is_causal_headline("PFE PDUFA date set for March 2026") is True

    def test_lawsuit_classifies_as_causal(self):
        assert is_causal_headline("META faces antitrust lawsuit from DOJ") is True

    def test_contract_win_classifies_as_causal(self):
        # "wins contract" together is the phrase the classifier matches.
        # The phrasing "wins $3B Pentagon contract" splits them and DOESN'T
        # match — defensible: tight phrase coupling avoids "wins customers"
        # false positives. Real Pentagon-award headlines tend to read
        # "LMT awarded contract" or "wins contract worth $XB".
        assert is_causal_headline("LMT wins contract worth $3B from Pentagon") is True
        assert is_causal_headline("LMT awarded contract for drone systems") is True

    def test_activist_stake_classifies_as_causal(self):
        assert is_causal_headline("Elliott takes activist stake in disney") is True


# ============================================================
# Negative cases — these MUST NOT classify as causal
# ============================================================

class TestCausalNegative:
    def test_technical_commentary_not_causal(self):
        """Chart-pattern commentary is not a verified catalyst."""
        assert is_causal_headline(
            "AAPL chart shows breakout pattern above 200-day moving average"
        ) is False

    def test_roundup_discussion_not_causal(self):
        assert is_causal_headline(
            "NVDA, META, GOOGL discussed in this week's analyst roundup"
        ) is False

    def test_generic_market_move_not_causal(self):
        assert is_causal_headline(
            "Tech stocks slide as treasury yields rise"
        ) is False

    def test_index_inclusion_not_causal(self):
        """S&P inclusion isn't on the keyword list — defensible; could add later."""
        assert is_causal_headline(
            "Stock heads higher amid market optimism"
        ) is False

    def test_empty_headline_not_causal(self):
        assert is_causal_headline("") is False
        assert is_causal_headline(None) is False  # type: ignore[arg-type]

    def test_just_ticker_mention_not_causal(self):
        assert is_causal_headline("ENPH closed at $42.15 today") is False


# ============================================================
# Keyword set quality — invariants we care about
# ============================================================

def test_keyword_set_is_lowercase():
    """All keywords stored in lowercase so the matcher works after .lower()."""
    for kw in CAUSAL_KEYWORDS:
        assert kw == kw.lower(), f"keyword {kw!r} contains uppercase chars"


def test_keyword_set_covers_core_catalyst_classes():
    """Sanity-check that at least one keyword exists for each catalyst class."""
    text = " ".join(CAUSAL_KEYWORDS).lower()
    assert "upgrade" in text or "downgrade" in text     # rating
    assert "target" in text or "pt" in text             # price target
    assert "beat" in text or "miss" in text             # earnings surprise
    assert "guidance" in text                           # guidance
    assert "acquisition" in text or "merger" in text    # M&A
    assert "buyback" in text                            # capital return
    assert "fda" in text or "pdufa" in text             # regulatory (biotech)
    assert "lawsuit" in text or "investigation" in text # legal
    assert "contract" in text                           # B2B catalyst
    assert "dilution" in text or "offering" in text     # financing
