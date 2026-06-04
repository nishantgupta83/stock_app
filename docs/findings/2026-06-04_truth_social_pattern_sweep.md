# Truth Social — 10-month pattern sweep

_Generated 2026-06-04._

One-shot analysis: where are the BUY-worthy patterns in the raw Trump post corpus, and what is the current classifier missing? Sweep window: requested last 10 months; actual corpus available 2026-04-22 → 2026-06-03.

## Corpus headline

| Metric | Value |
|---|---|
| Raw posts in window | 1088 |
| Posts that became classified events | 148 (13.6%) |
| Total normalized events produced (multi-ticker per post) | 274 |
| Closed paper trades on h7d from Trump posts | 125 |
| Posts UNCLASSIFIED (potential coverage gap) | 940 (86.4%) |

## BUY candidates — subtypes with highest profit factor (h7d, n≥5)

| subtype | n | win-rate | avg realized | PF | take |
|---|---|---|---|---|---|
| `tariff_general` | 42 | 73.8% | +1.39% | 4.40 | **STRONG BUY** |
| `china` | 16 | 50.0% | +1.98% | 2.60 | neutral |
| `djt_self` | 45 | 42.2% | +0.94% | 1.34 | neutral |
| `rates_dovish_or_hawkish` | 6 | 50.0% | -0.34% | 0.52 | AVOID |

## Pattern detection — labels found across raw posts

Each post is scanned for ~23 heuristic patterns. Posts can match multiple. The table below shows patterns sorted by paper-trade evidence (where it exists) — patterns that fired in posts that led to positive realized returns rank high.

| Pattern | Posts in corpus | Sample of outcomes (n) | avg realized % | take |
|---|---|---|---|---|
| `musk_or_tesla` | 3 | 39 | +3.50% | **BUY-worthy** |
| `threat_action` | 14 | 462 | +1.39% | **BUY-worthy** |
| `tariff_threat` | 17 | 714 | +1.39% | **BUY-worthy** |
| `criticize_company` | 3 | 126 | +1.39% | **BUY-worthy** |
| `question_mark` | 32 | 505 | +1.21% | **BUY-worthy** |
| `china_mention` | 17 | 479 | +1.17% | **BUY-worthy** |
| `union_topic` | 6 | 87 | +1.16% | **BUY-worthy** |
| `ALL_CAPS_RANT` | 200 | 762 | +1.14% | **BUY-worthy** |
| `political_attack` | 58 | 950 | +1.10% | **BUY-worthy** |
| `regulator_mention` | 26 | 234 | +1.08% | **BUY-worthy** |
| `exclamation_heavy` | 132 | 2153 | +1.01% | **BUY-worthy** |
| `election_topic` | 144 | 762 | +1.01% | **BUY-worthy** |
| `immigration_topic` | 108 | 183 | +0.84% | watch |
| `defense_topic` | 178 | 1141 | +0.81% | watch |
| `energy_topic` | 107 | 184 | +0.34% | AVOID |
| `praise_self` | 14 | 98 | +0.06% | AVOID |
| `praise_company` | 6 | 0 | +0.00% | neutral |
| `pardon_topic` | 1 | 0 | +0.00% | neutral |
| `crypto_topic` | 2 | 8 | -9.93% | AVOID |

## Coverage gap — unclassified posts by detected pattern

Of the unclassified posts (no current rule fired), these are the patterns most frequently detected — the strongest signal of where adding rules would unlock coverage:

| Pattern | Unclassified posts | Suggestion |
|---|---|---|
| `ALL_CAPS_RANT` | 155 | Noise feature — don't act standalone |
| `defense_topic` | 123 | Already covered (defense) |
| `election_topic` | 105 | Election cycles affect VIX broadly; consider adding XLV/VIX sentiment rule |
| `immigration_topic` | 82 | Added in 0037 (kwd0037_sector_immig) |
| `exclamation_heavy` | 76 | Noise feature — don't act standalone |
| `energy_topic` | 75 | Already covered (oil) |
| `political_attack` | 33 | Generally noise — no clear equity tradable signal |
| `question_mark` | 19 | Noise — Trump uses ? rhetorically |
| `regulator_mention` | 17 | Add Powell/Fed → TLT/XLF rules |
| `praise_self` | 8 | No tradable signal in isolation |
| `praise_company` | 5 | Combine with company name match → direction=long |
| `union_topic` | 4 | Consider adding UAW → GM/F short pattern |
| `threat_action` | 3 | Combine with target (company/sector) — already in tariff/regulation rules |
| `pardon_topic` | 1 | Add ticker-specific pardon detection (e.g., crypto pardons) |

## Concrete rule suggestions for a follow-up migration (0038)

Based on the patterns above, these new rules would close the biggest coverage gaps:

```sql
-- CEO-name → ticker aliases (mentioned often, no current rule)
insert into stock_keyword_rules (kind, enabled, keyword, match_type, direction_prior, tickers, rule_label) values
  ('truth_social', true, 'tim cook',     'icontains', 'neutral', '{AAPL}',  'kwd0038_ceo_cook'),
  ('truth_social', true, 'sundar pichai','icontains', 'neutral', '{GOOGL}', 'kwd0038_ceo_pichai'),
  ('truth_social', true, 'zuckerberg',   'icontains', 'neutral', '{META}',  'kwd0038_ceo_zuck'),
  ('truth_social', true, 'jamie dimon',  'icontains', 'neutral', '{JPM}',   'kwd0038_ceo_dimon'),
  ('truth_social', true, 'powell',       'icontains', 'long',    '{TLT,XLF}','kwd0038_powell_fed'),
  ('truth_social', true, 'uaw|teamsters','regex',     'short',   '{GM,F}',   'kwd0038_union_auto');
```

Ship via `sql/0038_truth_social_round2.sql` after operator review.

## Caveats — what NOT to read into this

- Sample sizes per pattern are small. Several patterns have <10 trades; conclusions are directional, not authoritative.
- The h7d window can be dominated by macro moves unrelated to the post (especially with 'china_mention' and 'tariff_threat' patterns where market-wide news drives the move).
- Win-rate ≠ profitability. A pattern with 70% win-rate but tiny wins and rare-but-big losses can still net negative.
- This is a one-shot snapshot. Re-run after another month of data to see if patterns hold.

**Re-run:** `python3 scripts/truth_social_pattern_sweep.py` (idempotent — overwrites this doc).