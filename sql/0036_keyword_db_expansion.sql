-- 0036 — news classifier keyword DB expansion
--
-- Context (2026-06-02): rejection audit + dashboard showed the news
-- classifier marks 85% of articles as neutral. Inspecting the table found
-- only 24 rules total — 22 ticker-name matchers + 1 bullish regex + 1
-- bearish regex. The bullish regex covered ~10 keywords (`beat(s)?`,
-- `jumps?`, `rally`, `surge`, `raises guidance`, etc.). NVDA's 6/2 Computex
-- coverage was classified neutral because the regex doesn't recognize
-- analyst-firm raises, AI partnership announcements, regulatory wins, or
-- geopolitical risks — all common catalyst classes.
--
-- This migration adds ~40 high-specificity catalyst phrases. Each rule:
--   * is icontains (case-insensitive substring) unless ambiguity forces regex
--   * has a unique rule_label so we can A/B which ones add signal vs noise
--   * is enabled=true; failed rules can be disabled later via a one-row
--     PATCH without a deploy
--
-- Design constraints to avoid false positives:
--   * No generic verbs ("positive", "negative", "rally" alone are too broad)
--   * Require 2+ words of context per rule
--   * Phrases that ONLY appear in financial-news contexts
--
-- Reversal: each row is independently enable-able; to fully undo, set
-- enabled=false where rule_label like 'kwd0036_%' OR delete those rows.

insert into stock_keyword_rules (kind, enabled, keyword, match_type, direction_prior, rule_label, notes) values

-- A. Price target changes (bullish)
('news', true, 'raises price target', 'icontains', 'long',  'kwd0036_pt_raised_1',  '0036: PT raise wording 1'),
('news', true, 'lifts price target',  'icontains', 'long',  'kwd0036_pt_raised_2',  '0036: PT raise wording 2'),
('news', true, 'price target raised', 'icontains', 'long',  'kwd0036_pt_raised_3',  '0036: PT raise wording 3'),
('news', true, 'street-high target',  'icontains', 'long',  'kwd0036_pt_raised_4',  '0036: bullish high target'),
('news', true, 'street-high pt',      'icontains', 'long',  'kwd0036_pt_raised_5',  '0036: bullish high pt'),
('news', true, 'raises pt to',        'icontains', 'long',  'kwd0036_pt_raised_6',  '0036: PT raise with target'),

-- B. Price target changes (bearish)
('news', true, 'cuts price target',   'icontains', 'short', 'kwd0036_pt_cut_1',     '0036: PT cut wording 1'),
('news', true, 'lowers price target', 'icontains', 'short', 'kwd0036_pt_cut_2',     '0036: PT cut wording 2'),
('news', true, 'price target cut',    'icontains', 'short', 'kwd0036_pt_cut_3',     '0036: PT cut wording 3'),
('news', true, 'cuts pt to',          'icontains', 'short', 'kwd0036_pt_cut_4',     '0036: PT cut with target'),

-- C. Analyst initiation (bullish)
('news', true, 'initiated buy',           'icontains', 'long',  'kwd0036_init_buy_1', '0036: analyst init buy'),
('news', true, 'initiates with buy',      'icontains', 'long',  'kwd0036_init_buy_2', '0036: analyst init buy v2'),
('news', true, 'initiated outperform',    'icontains', 'long',  'kwd0036_init_buy_3', '0036: analyst init outperform'),
('news', true, 'initiates with outperform','icontains','long', 'kwd0036_init_buy_4', '0036: analyst init outperform v2'),

-- D. Analyst initiation / downgrade (bearish)
('news', true, 'initiated sell',          'icontains', 'short', 'kwd0036_init_sell_1', '0036: analyst init sell'),
('news', true, 'initiated underperform',  'icontains', 'short', 'kwd0036_init_sell_2', '0036: analyst init underperform'),
('news', true, 'downgraded to sell',      'icontains', 'short', 'kwd0036_downgrade_1', '0036: downgrade to sell'),
('news', true, 'downgraded to underperform','icontains','short','kwd0036_downgrade_2', '0036: downgrade to underperform'),
('news', true, 'cut to underperform',     'icontains', 'short', 'kwd0036_downgrade_3', '0036: cut to underperform'),

-- E. AI / cloud catalysts (bullish)
('news', true, 'ai partnership',          'icontains', 'long',  'kwd0036_ai_partner',    '0036: AI partnership'),
('news', true, 'ai contract win',         'icontains', 'long',  'kwd0036_ai_contract',   '0036: AI contract win'),
('news', true, 'wins ai deal',            'icontains', 'long',  'kwd0036_ai_deal',       '0036: AI deal win'),
('news', true, 'multi-billion ai',        'icontains', 'long',  'kwd0036_ai_billion',    '0036: multi-billion AI'),
('news', true, 'gpu allocation',          'icontains', 'long',  'kwd0036_gpu_alloc',     '0036: GPU allocation award'),
('news', true, 'hyperscaler deal',        'icontains', 'long',  'kwd0036_hyperscaler',   '0036: hyperscaler deal'),

-- F. FDA / Biotech catalysts (bullish)
('news', true, 'breakthrough designation','icontains', 'long',  'kwd0036_fda_breakthrough','0036: FDA breakthrough'),
('news', true, 'priority review',         'icontains', 'long',  'kwd0036_fda_priority',  '0036: FDA priority review'),
('news', true, 'met primary endpoint',    'icontains', 'long',  'kwd0036_endpoint_met',  '0036: trial endpoint met'),
('news', true, 'topline data positive',   'icontains', 'long',  'kwd0036_topline_pos',   '0036: positive topline'),
('news', true, 'ema approval',            'icontains', 'long',  'kwd0036_ema_approval',  '0036: EMA approval'),
('news', true, 'fast track designation',  'icontains', 'long',  'kwd0036_fast_track',    '0036: FDA fast track'),
('news', true, 'orphan drug designation', 'icontains', 'long',  'kwd0036_orphan_drug',   '0036: orphan drug'),

-- G. FDA / Biotech (bearish)
('news', true, 'missed primary endpoint', 'icontains', 'short', 'kwd0036_endpoint_missed','0036: trial endpoint missed'),
('news', true, 'failed primary endpoint', 'icontains', 'short', 'kwd0036_endpoint_failed','0036: trial endpoint failed'),
('news', true, 'clinical hold',           'icontains', 'short', 'kwd0036_clinical_hold', '0036: clinical hold imposed'),
('news', true, 'trial halt',              'icontains', 'short', 'kwd0036_trial_halt',    '0036: trial halted'),
('news', true, 'enrollment suspended',    'icontains', 'short', 'kwd0036_enroll_susp',   '0036: enrollment suspended'),

-- H. Geopolitical / trade (bearish)
('news', true, 'new tariff',              'icontains', 'short', 'kwd0036_tariff_new',    '0036: new tariff'),
('news', true, 'tariff threat',           'icontains', 'short', 'kwd0036_tariff_threat', '0036: tariff threat'),
('news', true, 'export controls',         'icontains', 'short', 'kwd0036_export_ctrl',   '0036: export controls'),
('news', true, 'chip ban',                'icontains', 'short', 'kwd0036_chip_ban',      '0036: chip export ban'),
('news', true, 'sanctions imposed',       'icontains', 'short', 'kwd0036_sanctions',     '0036: sanctions'),

-- I. Operational risk (bearish)
('news', true, 'regulatory probe',        'icontains', 'short', 'kwd0036_reg_probe',     '0036: regulatory probe'),
('news', true, 'cyberattack',             'icontains', 'short', 'kwd0036_cyber',         '0036: cyberattack'),
('news', true, 'data breach disclosed',   'icontains', 'short', 'kwd0036_data_breach',   '0036: data breach'),
('news', true, 'production halt',         'icontains', 'short', 'kwd0036_prod_halt',     '0036: production halt');

-- Sanity: how many rules now in the news kind?
comment on table stock_keyword_rules is
  'News classifier rules. As of 0036 (2026-06-02): 24 baseline + ~45 catalyst phrases. '
  'Disable a single rule with: update stock_keyword_rules set enabled=false where rule_label=$1; '
  'No deploy required — news_agent reloads on next run.';
