# Jev トリアージ助言ドラフト

> この文書は Jev の回答から機械的に作った助言ドラフトであり、最終 disposition ではない。Fix / Skip / Hold / Resolved の確定、検証済みマークの付与、PR・Issue への投稿、コード修正は、既存の review-fix-planning 手順で人間またはエージェントが行う。

## 概要

| 項目 | 値 |
| --- | --- |
| status | `advisory` |
| reason | `advisory: 2 of 4 active finding(s) have an advisory proposal; unparsed blocks: 1 (not triaged; check the review files); input anomalies: 1` |
| repository | `usaturn/xrift-poc` |
| review_dir | `reviews/#999-feature-example` |
| reviewed_head | `fdfc294f57fb773d9024d041d834e7ede1c7cd78` |
| model_id_requested | `jev-latest` |
| model_id_responded | `jev-1.13.0` |
| question_set_version | `1` |
| thresholds | `model_id=jev-latest choice>=0.9 score>=0.9 noul_yes>=0.95 noul_no<=0.05` |
| unparsed_count | `1` |
| decided_count | `0` |
| unverified_count | `0` |
| generated_at | `2026-09-26T00:00:00Z` |

> 解釈できなかったブロックが 1 件ある。これらはトリアージしていないため、元レビューを review-fix-planning 手順で確認する。

## 提案の内訳

| 提案 | 件数 |
| --- | --- |
| `fix_now` | 1 |
| `separate_issue` | 1 |
| `reject` | 0 |
| `human_review` | 3 |

`human_review` の指摘は、既存の review-fix-planning 手順で人間またはエージェントが判断する。

## 指摘一覧

| # | finding_id | 提案 | severity（最高 / 最低 / 一致） | 重複候補 | 理由 |
| --- | --- | --- | --- | --- | --- |
| 1 | `alpha/active/H-1` | `fix_now` | `high` / `medium` / 不一致 | 1 | 0 |
| 2 | `beta/active/M-1` | `human_review` | `high` / `low` / 不一致 | 2 | 1 |
| 3 | `gamma/active/L-1` | `human_review` | `medium` / `low` / 不一致 | 1 | 2 |
| 4 | `delta/active/I-1` | `separate_issue` | `info` / `info` / 一致 | 0 | 0 |
| 5 | `alpha/resolved/L-2` | `human_review` | `low` / `low` / 一致 | 0 | 1 |

## 指摘ごとの詳細

### 1. `alpha/active/H-1`

- 提案: `fix_now`（助言）
- severity: 最高 `high` / 最低 `medium` / 不一致
  - `alpha/active/H-1`: `high`
  - `beta/active/M-1`: `medium`
- 重複候補:
  - `beta/active/M-1`
- エスカレーション理由: なし
- 根拠参照:
  - `review:alpha.md#L10-L16`
  - `location:tools/example.py:10`
  - `reviewed_head:fdfc294f57fb`
  - `detected_head:fdfc294f57fb`
  - `request:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`
  - `request:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee`
  - `response:req-a`
- 回答:
  - `applies_d40ab58ac648`: noul `1.0`
  - `proposal_d40ab58ac648`: choice `fix_now` confidence `0.95`
  - `same_53e99ccc3161_d40ab58ac648`: noul `0.97`

### 2. `beta/active/M-1`

- 提案: `human_review`（助言）
- severity: 最高 `high` / 最低 `low` / 不一致
  - `beta/active/M-1`: `medium`
  - `alpha/active/H-1`: `high`
  - `gamma/active/L-1`: `low`
- 重複候補:
  - `alpha/active/H-1`
  - `gamma/active/L-1`
- エスカレーション理由:
  - `duplicate_uncertain: partner=gamma/active/L-1 noul=0.5`
- 根拠参照:
  - `review:beta.md#L10-L16`
  - `location:tools/example.py:10`
  - `reviewed_head:fdfc294f57fb`
  - `detected_head:fdfc294f57fb`
  - `request:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb`
  - `request:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee`
  - `request:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff`
- 回答:
  - `applies_53e99ccc3161`: noul `1.0`
  - `proposal_53e99ccc3161`: choice `fix_now` confidence `0.95`
  - `same_3a520c9eac8e_53e99ccc3161`: noul `0.5`
  - `same_53e99ccc3161_d40ab58ac648`: noul `0.97`

### 3. `gamma/active/L-1`

- 提案: `human_review`（助言）
- severity: 最高 `medium` / 最低 `low` / 不一致
  - `gamma/active/L-1`: `low`
  - `beta/active/M-1`: `medium`
- 重複候補:
  - `beta/active/M-1`
- エスカレーション理由:
  - `proposal_low_confidence: proposal_3a520c9eac8e confidence=0.5 < 0.9`
  - `duplicate_uncertain: partner=beta/active/M-1 noul=0.5`
- 根拠参照:
  - `review:gamma.md#L10-L16`
  - `location:tools/example.py:10`
  - `reviewed_head:fdfc294f57fb`
  - `detected_head:fdfc294f57fb`
  - `request:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc`
  - `request:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff`
- 回答:
  - `applies_3a520c9eac8e`: noul `1.0`
  - `proposal_3a520c9eac8e`: choice `fix_now` confidence `0.5`
  - `same_3a520c9eac8e_53e99ccc3161`: noul `0.5`

### 4. `delta/active/I-1`

- 提案: `separate_issue`（助言）
- severity: 最高 `info` / 最低 `info` / 一致
  - `delta/active/I-1`: `info`
- 重複候補: なし
- エスカレーション理由: なし
- 根拠参照:
  - `review:delta.md#L10-L16`
  - `location:tools/example.py:10`
  - `reviewed_head:fdfc294f57fb`
  - `detected_head:fdfc294f57fb`
  - `request:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd`
- 回答:
  - `applies_34f7243e5dba`: noul `1.0`
  - `proposal_34f7243e5dba`: choice `separate_issue` confidence `0.93`

### 5. `alpha/resolved/L-2`

- 提案: `human_review`（助言）
- severity: 最高 `low` / 最低 `low` / 一致
  - `alpha/resolved/L-2`: `low`
- 重複候補: なし
- エスカレーション理由:
  - `status_resolved`
- 根拠参照:
  - `review:alpha.md#L40-L46`
  - `location:tools/example.py:10`
  - `reviewed_head:fdfc294f57fb`
  - `detected_head:fdfc294f57fb`
- 回答: なし
