-- 0037 — truth_social keyword expansion
--
-- The truth_social classifier currently has 25 rules: 6 thematic (china,
-- tariff, oil, rates, crypto, djt_self) and 19 company name matchers
-- (mega caps only). When the user requested "make sure it's not just
-- focused on China but overall posts, like he said buy Dell stock", the
-- audit revealed:
--   * No Dell, Boeing, Disney, Ford, GM, IBM, Cisco, etc.
--   * No thematic rules for steel/aluminum, autos, defense, AI policy,
--     bank regulation, social media regulation.
--   * No endorsement / criticism vocabulary ("buy X", "X is failing").
--
-- This migration adds ~35 new rules:
--   A. 18 additional name matchers (mid+large caps Trump comments on)
--   B. 9 sector / policy thematic rules
--   C. 8 endorsement / criticism vocabulary
--
-- Same idempotency pattern as 0036: each rule has a unique rule_label
-- (kwd0037_*) so a misfire can be disabled with a one-row PATCH.
--
-- Reversal: set enabled=false WHERE rule_label LIKE 'kwd0037_%'.

insert into stock_keyword_rules (kind, enabled, keyword, match_type, direction_prior, tickers, rule_label, notes) values

-- A. Additional name matchers — companies Trump frequently posts about
('truth_social', true, 'dell',           'icontains', 'neutral', '{DELL}',         'kwd0037_name_dell',         '0037: Dell Technologies'),
('truth_social', true, 'boeing',         'icontains', 'neutral', '{BA}',           'kwd0037_name_ba',           '0037: Boeing'),
('truth_social', true, 'general motors', 'icontains', 'neutral', '{GM}',           'kwd0037_name_gm_long',      '0037: GM full name'),
('truth_social', true, '\bgm\b',         'regex',     'neutral', '{GM}',           'kwd0037_name_gm_short',     '0037: GM ticker mention'),
('truth_social', true, 'ford motor',     'icontains', 'neutral', '{F}',            'kwd0037_name_f_long',       '0037: Ford full name'),
('truth_social', true, 'lockheed',       'icontains', 'neutral', '{LMT}',          'kwd0037_name_lmt',          '0037: Lockheed Martin'),
('truth_social', true, 'caterpillar',    'icontains', 'neutral', '{CAT}',          'kwd0037_name_cat',          '0037: Caterpillar'),
('truth_social', true, 'goldman sachs',  'icontains', 'neutral', '{GS}',           'kwd0037_name_gs',           '0037: Goldman Sachs'),
('truth_social', true, 'blackrock',      'icontains', 'neutral', '{BLK}',          'kwd0037_name_blk',          '0037: BlackRock'),
('truth_social', true, 'disney',         'icontains', 'neutral', '{DIS}',          'kwd0037_name_dis',          '0037: Disney'),
('truth_social', true, 'mcdonalds',      'icontains', 'neutral', '{MCD}',          'kwd0037_name_mcd',          '0037: McDonalds (one apostrophe variant)'),
('truth_social', true, 'starbucks',      'icontains', 'neutral', '{SBUX}',         'kwd0037_name_sbux',         '0037: Starbucks'),
('truth_social', true, 'intel',          'icontains', 'neutral', '{INTC}',         'kwd0037_name_intc',         '0037: Intel'),
('truth_social', true, 'ibm',            'icontains', 'neutral', '{IBM}',          'kwd0037_name_ibm',          '0037: IBM'),
('truth_social', true, 'cisco',          'icontains', 'neutral', '{CSCO}',         'kwd0037_name_csco',         '0037: Cisco'),
('truth_social', true, 'qualcomm',       'icontains', 'neutral', '{QCOM}',         'kwd0037_name_qcom',         '0037: Qualcomm'),
('truth_social', true, 'oracle',         'icontains', 'neutral', '{ORCL}',         'kwd0037_name_orcl',         '0037: Oracle'),
('truth_social', true, 'salesforce',     'icontains', 'neutral', '{CRM}',          'kwd0037_name_crm',          '0037: Salesforce'),

-- B. Sector / policy thematic rules
('truth_social', true, '\b(steel|aluminum)\s+(tariff|imports|dumping)', 'regex', 'long',  '{X,CLF,NUE}',     'kwd0037_sector_steel',    '0037: steel/aluminum protectionism -> domestic steel long'),
('truth_social', true, '\bauto\s+(industry|tariff|imports)|car\s+manufact',     'regex', 'long',  '{GM,F,TSLA}',     'kwd0037_sector_auto',     '0037: auto industry policy'),
('truth_social', true, '\bdefense\s+(spending|budget|contract)|nato',           'regex', 'long',  '{LMT,RTX,NOC,GD}','kwd0037_sector_defense',  '0037: defense spending -> defense contractors'),
('truth_social', true, '\bai\s+(safety|regulation|executive\s+order|ban)',      'regex', 'short', '{NVDA,AMD,GOOGL}', 'kwd0037_sector_ai_reg',   '0037: AI regulation chatter -> AI stocks short'),
('truth_social', true, '\bbank(s|ing)\s+(regulation|bailout|too\s+big)',        'regex', 'short', '{XLF,JPM,BAC,GS}','kwd0037_sector_bank_reg', '0037: bank regulation talk -> XLF short'),
('truth_social', true, '\b(social\s+media|big\s+tech)\s+(censorship|ban|regulat)','regex','short','{META,GOOGL,SNAP}','kwd0037_sector_social',   '0037: social media regulation rhetoric'),
('truth_social', true, '\bimmigration\s+(crackdown|enforcement|deport)',        'regex', 'short', '{XLY,CHWY,COST}', 'kwd0037_sector_immig',    '0037: immigration policy -> consumer discretionary risk'),
('truth_social', true, '\benergy\s+(independence|dominance|production)|drill',  'regex', 'long',  '{XLE,XOM,CVX,OXY}','kwd0037_sector_energy_dom','0037: energy independence -> domestic energy long'),
('truth_social', true, '\bsemiconductor|chip\s+act|tsmc',                       'regex', 'long',  '{NVDA,AMD,INTC,AVGO}','kwd0037_sector_semi', '0037: semiconductor policy'),

-- C. Endorsement / criticism vocabulary (works in conjunction with name matchers)
('truth_social', true, '\b(buy|own|invest\s+in)\s+(stock|shares)',              'regex', 'long',  '{}', 'kwd0037_verb_buy_stock',  '0037: explicit buy verb; ticker pairing comes from other rules'),
('truth_social', true, '\b(sell|short|avoid)\s+(stock|shares)',                 'regex', 'short', '{}', 'kwd0037_verb_sell_stock', '0037: explicit sell verb'),
('truth_social', true, '\b(great|tremendous|winning|booming|strong)\s+(company|stock|business)', 'regex', 'long',  '{}', 'kwd0037_verb_endorse',     '0037: praise pattern'),
('truth_social', true, '\b(failing|disaster|terrible|weak|losing\s+money)\s+(company|stock|business)', 'regex', 'short', '{}', 'kwd0037_verb_criticize',  '0037: criticism pattern'),
('truth_social', true, '\b(should\s+be\s+(banned|broken\s+up|investigated))',   'regex', 'short', '{}', 'kwd0037_verb_break_up',   '0037: regulatory threat verb'),
('truth_social', true, '\b(record|all[\s-]time)\s+(high|profit|earnings)',      'regex', 'long',  '{}', 'kwd0037_verb_record_high','0037: record-setting language'),
('truth_social', true, '\b(crash|collapse|going\s+to\s+zero)',                  'regex', 'short', '{}', 'kwd0037_verb_crash',      '0037: catastrophic language'),
('truth_social', true, '\b(pardon|presidential\s+pardon)',                       'regex', 'long',  '{}', 'kwd0037_verb_pardon',     '0037: pardon discussion — historically equity-positive for named co');

comment on table stock_keyword_rules is
  'News + truth_social classifier rules. As of 0037 (2026-06-03): 70 news + ~60 truth_social. '
  'Disable any rule via: update stock_keyword_rules set enabled=false where rule_label=$1; '
  'No deploy required — agents reload rules on each run.';
