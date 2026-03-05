# DDI フィルター質問対応（C2）実装まとめ

## 概要

調査票には「Q3で『はい』と答えた方のみQ4へ」のような
**スキップ指示（フィルター条件）** が存在する。

従来の実装では `variables` スキーマが
`name` / `label` / `question` / `categories` / `format` の5フィールドのみであり、
変数間のフィルター条件を保持する手段がなかった。
また、DDI-Codebook 2.5 が `<var><qstn><filtrInstr>` でフィルター条件を表現できるにもかかわらず、
XML 出力にも `<filtrInstr>` は含まれていなかった。

本実装では以下の2点を追加した（C2）。

1. **スキーマ拡張**: `variables` に `filter_condition` フィールドを追加し、プロンプトで抽出を指示
2. **XML 出力拡張**: `_build_var()` を修正し、`<filtrInstr>` 要素を DDI XML に出力

---

## 変更ファイル一覧

| ファイル | 変更内容 |
|---|---|
| `src/curategpt/agents/paper_to_ddi_agent.py` | `EXTRACTION_GROUPS["survey"]["instructions"]` に `filter_condition` フィールド追加 |
| `src/curategpt/wrappers/social/ddi_xml_serializer.py` | `_build_var()` docstring 更新・`<filtrInstr>` 出力ロジック追加 |
| `tests/wrappers/test_ddi_xml_serializer_c2.py` | 新規テストファイル（13ケース） |
| `DDI_PIPELINE_COMPLETE.md` | C2 を ✅ 実装済みに更新 |

---

## 実装詳細

### 1. `EXTRACTION_GROUPS["survey"]["instructions"]` への `filter_condition` 追加

```python
# 変更前
"'format' (numeric|string|date). "
"Extract ALL variables present in the provided text — do not skip any. "

# 変更後
"'format' (numeric|string|date), "
"'filter_condition' (string describing the skip/filter instruction for this "
"variable, e.g. \"Q3で『はい』と答えた方のみ\" or \"Skip to Q5 if No\"; "
"null if the question is asked unconditionally). "
"Extract ALL variables present in the provided text — do not skip any. "
```

これにより LLM は以下の形式で `variables` を返すようになる。

```yaml
variables:
  - name: Q3
    label: Q3: 配偶者の有無
    question: 配偶者はいますか。
    categories:
      - {value: "1", label: はい}
      - {value: "2", label: いいえ}
    format: numeric
    filter_condition: null          # 無条件

  - name: Q4
    label: Q4: 配偶者の職業
    question: 配偶者のご職業を教えてください。
    categories:
      - {value: "1", label: 会社員}
      - {value: "2", label: 自営業}
    format: numeric
    filter_condition: "Q3で『はい』と答えた方のみ"   # フィルター条件
```

---

### 2. `_build_var()` の変更（`ddi_xml_serializer.py`）

#### コード（変更前）

```python
# <qstn><qstnLit> — question text
question = _str_or_none(var.get("question"))
if question:
    qstn = _sub(var_el, "qstn")
    _sub(qstn, "qstnLit", text=question)
```

#### コード（変更後）

```python
# <qstn><qstnLit> and/or <filtrInstr> — question text + filter condition (C2)
question = _str_or_none(var.get("question"))
filter_cond = _str_or_none(var.get("filter_condition"))
if question or filter_cond:
    qstn = _sub(var_el, "qstn")
    if question:
        _sub(qstn, "qstnLit", text=question)
    if filter_cond:
        _sub(qstn, "filtrInstr", text=filter_cond)
```

#### docstring への `filter_condition` 追記

```python
"""Build a ``<var>`` element from a variable dict.

Expected variable dict keys:

* ``name``             — short variable code (e.g. ``"Q1"``, ``"SEX"``)
* ``label``            — full variable label
* ``question``         — verbatim question text
* ``filter_condition`` — skip/filter instruction (e.g. "Q3で『はい』の方のみ")
* ``categories``       — list of ``{"value": ..., "label": ...}`` dicts
* ``format``           — ``"numeric"`` / ``"character"`` / ``"date"``
"""
```

---

## XML 出力イメージ

### 通常の質問（フィルターなし）

```xml
<var ID="V3" name="Q3">
  <labl>Q3: 配偶者の有無</labl>
  <qstn>
    <qstnLit>配偶者はいますか。</qstnLit>
    <!-- <filtrInstr> なし -->
  </qstn>
  <catgry><catValu>1</catValu><labl>はい</labl></catgry>
  <catgry><catValu>2</catValu><labl>いいえ</labl></catgry>
  <varFormat type="numeric"/>
</var>
```

### フィルター付き質問（question + filter_condition 両方あり）

```xml
<var ID="V4" name="Q4">
  <labl>Q4: 配偶者の職業</labl>
  <qstn>
    <qstnLit>配偶者のご職業を教えてください。</qstnLit>
    <filtrInstr>Q3で『はい』と答えた方のみ</filtrInstr>
  </qstn>
  <catgry><catValu>1</catValu><labl>会社員</labl></catgry>
  <catgry><catValu>2</catValu><labl>自営業</labl></catgry>
  <varFormat type="numeric"/>
</var>
```

### question なし・filter_condition のみ

```xml
<var ID="V5" name="Q5">
  <labl>Q5: 自由回答</labl>
  <qstn>
    <!-- <qstnLit> なし -->
    <filtrInstr>Q4で『その他』を選んだ方のみ</filtrInstr>
  </qstn>
  <varFormat type="character"/>
</var>
```

---

## テスト結果

### `TestBuildVarFilterCondition` — 10ケース（`_build_var()` の `<filtrInstr>` 出力）

| テスト内容 | 結果 |
|---|---|
| `filter_condition` → `<filtrInstr>` 出力 | ✅ |
| `question` + `filter_condition` → `<qstnLit>` と `<filtrInstr>` が共存 | ✅ |
| `question`=None + `filter_condition` → `<qstn>` に `<filtrInstr>` のみ（`<qstnLit>` なし） | ✅ |
| 英語スキップ指示（"Skip to Q5b if Yes to Q5"）が正しく出力 | ✅ |
| `filter_condition` キー不在 → `<filtrInstr>` なし | ✅ |
| `filter_condition`=None → `<filtrInstr>` なし | ✅ |
| `question`=None かつ `filter_condition`=None → `<qstn>` 要素自体なし | ✅ |
| `filter_condition` の有無が `<labl>` に影響しない | ✅ |
| `filter_condition` の有無が `<catgry>` に影響しない | ✅ |
| 複数変数で一部のみ `filter_condition` → 該当変数のみ `<filtrInstr>` | ✅ |

### `TestSurveyInstructionsFilterCondition` — 3ケース（プロンプト記述の確認）

| テスト内容 | 結果 |
|---|---|
| survey instructions に `filter_condition` キーの記述がある | ✅ |
| instructions にスキップ/フィルターの例示がある | ✅ |
| instructions に「null（無条件）」の指示がある | ✅ |

**合計 13/13 PASSED**

---

## 設計上の判断

### `<filtrInstr>` の配置（DDI-Codebook 2.5）

DDI-Codebook 2.5 では `<filtrInstr>` は `<var>` の直下ではなく
`<var><qstn>` の子要素として定義されている。
`<qstnLit>`（質問文）と同一の `<qstn>` コンテナに収まるため、
「この質問に対するフィルター条件」という意味的まとまりが XML 構造上も表現される。

### `question` なし・`filter_condition` あり の対応

調査票の一部の設問では、設問文がスキップ条件の中に含まれているため、
LLM が `question` を null と判断し `filter_condition` のみを返す場合がある。
この場合も `<qstn>` 要素を生成し `<filtrInstr>` を格納することで、
フィルター情報が失われない。

### 後方互換性

- `filter_condition` キーを持たない既存の `variables` データは変更不要。
  `var.get("filter_condition")` が None を返すため、`<filtrInstr>` は生成されず従来と同じ XML が出力される。
- `extract_ddi_from_text()` / `extract_ddi_from_files()` の API シグネチャは変更なし。

### LLM への期待動作

フィルター条件がある場合、LLM が調査票テキストから以下を抽出することを期待する：

| パターン例 | 期待される `filter_condition` |
|---|---|
| `Q3で「はい」と答えた方のみQ4へ` | `"Q3で「はい」と答えた方のみ"` |
| `（Q2で「2」を選択した方のみ）` | `"Q2で「2」を選択した方のみ"` |
| `Skip to Q5 if No to Q4` | `"Skip to Q5 if No to Q4"` |
| `Only if employed (Q7=1)` | `"Only if employed (Q7=1)"` |
| 無条件の設問 | `null` |

---

## 関連ドキュメント

| ファイル | 内容 |
|---|---|
| `DDI_PIPELINE_COMPLETE.md` | DDI 生成パイプライン全体の手順と課題（C2 ✅ に更新済み） |
| `DDI_CHUNKED_SURVEY_SUMMARY.md` | C3 変数件数上限対応（`_QUESTION_BOUNDARY_RE` の定義元） |
| `DDI_UNSTRUCTURED_SURVEY_SUMMARY.md` | B3 非構造化調査票対応 |
| `DDI_HALLUCINATION_GUARD_SUMMARY.md` | C1 Hallucination 対策の実装詳細 |
