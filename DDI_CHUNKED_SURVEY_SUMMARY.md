# DDI 変数件数上限対応（C3）実装まとめ

## 概要

大規模調査（変数300件以上）でも後半の変数が欠落しないよう、
`survey` グループのテキストを質問境界で自動分割し、
ブロックごとに LLM を呼び出して結果を結合する分割処理を実装した（C3）。

従来は `EXTRACTION_GROUPS["survey"]["instructions"]` に
「Limit to at most 100 variables」の上限が設けられており、
100件を超える変数は抽出されなかった。
本実装により、変数数に上限を設けず全件抽出できるようになった。

---

## 変更ファイル一覧

| ファイル | 変更内容 |
|---|---|
| `src/curategpt/agents/paper_to_ddi_agent.py` | `_VARS_PER_BLOCK` 定数・`_QUESTION_BOUNDARY_RE` 正規表現・`vars_per_block` フィールド・`_split_survey_blocks()` 新規・`_extract_survey_variables_chunked()` 新規・`_run_extraction_groups()` の `survey` グループ処理切り替え・プロンプト上限記述削除 |
| `tests/agents/test_paper_to_ddi_c3.py` | 新規テストファイル（26ケース） |
| `DDI_PIPELINE_COMPLETE.md` | C3 を ✅ 実装済みに更新 |

---

## 実装詳細

### 1. `_VARS_PER_BLOCK` 定数と `_QUESTION_BOUNDARY_RE` 正規表現（モジュールレベル）

```python
# デフォルトブロックサイズ（LLM 1回あたりの最大変数数）
_VARS_PER_BLOCK: int = 50

# 質問境界検出正規表現
_QUESTION_BOUNDARY_RE = re.compile(
    r"(?m)^"                              # 行頭（multiline モード）
    r"(?:\|\s*)?"                         # 任意のパイプ（A2 テーブル行）
    r"(?:"
    r"(?:SQ|F|Q|q)\s*\d+"               # Q1, F2, SQ3
    r"|問\s*\d+"                          # 問1, 問 2
    r"|第\s*\d+\s*問"                    # 第1問, 第 2 問
    r")"
    r"(?=[\s\.\)．。\|]|$)",             # 先読み：区切り文字または行末
)
```

#### 対応する質問番号パターン

| パターン | 例 |
|---|---|
| 英語 Q スタイル | `Q1.`, `Q 12`, `q3)` |
| フォーム変数コード | `F1.`, `F 2` |
| サブ質問コード | `SQ1.`, `SQ 3` |
| 日本語 問N | `問1`, `問 2.` |
| 日本語 第N問 | `第1問`, `第 2 問` |
| A2 パイプ表形式 | `\| Q1 \|`, `\| 問3 \|` |

---

### 2. `vars_per_block` フィールド（`PaperToDDIAgent` データクラス）

```python
#: Maximum number of survey questions to extract per LLM call (C3).
#: When the questionnaire text contains more detected question boundaries
#: than this value, it is automatically split into consecutive blocks and
#: the LLM is called once per block.  The resulting ``variables`` lists
#: are concatenated.  Increase for models with larger context windows.
vars_per_block: int = _VARS_PER_BLOCK   # デフォルト 50
```

---

### 3. `_split_survey_blocks(text)` — テキスト分割メソッド（新規）

`_QUESTION_BOUNDARY_RE` で質問境界を検出し、
`vars_per_block` 件ごとにテキストを分割してブロックのリストを返す。

#### 処理フロー

```
survey_items テキスト
    │
    ├─ _QUESTION_BOUNDARY_RE.finditer() → 境界位置リスト
    │
    ├─ 境界数 ≤ vars_per_block
    │    → [text] を返す（分割なし、従来動作と同じ）
    │
    └─ 境界数 > vars_per_block
         split_points = [0, positions[50], positions[100], ..., len(text)]
         → text[split_points[i] : split_points[i+1]] を各ブロックとして返す
```

#### ブロック分割例（vars_per_block=50、120変数の場合）

```
ブロック1: Q1  〜 Q50  のテキスト  （+ 先頭のプレアンブルも含む）
ブロック2: Q51 〜 Q100 のテキスト
ブロック3: Q101〜 Q120 のテキスト
```

#### コード（抜粋）

```python
def _split_survey_blocks(self, text: str) -> List[str]:
    matches = list(_QUESTION_BOUNDARY_RE.finditer(text))

    if len(matches) <= self.vars_per_block:
        return [text]   # 分割不要

    positions = [m.start() for m in matches]
    split_points = [0]
    for i in range(self.vars_per_block, len(positions), self.vars_per_block):
        split_points.append(positions[i])
    split_points.append(len(text))

    blocks = []
    for i in range(len(split_points) - 1):
        block = text[split_points[i] : split_points[i + 1]]
        if block.strip():
            blocks.append(block.strip())
    return blocks
```

---

### 4. `_extract_survey_variables_chunked(section_text, instructions)` — チャンク抽出メソッド（新規）

`_split_survey_blocks()` でブロックを取得し、
ブロック数が 1 の場合は従来の `_extract_group()` を呼ぶ。
2ブロック以上の場合はブロックごとに `_extract_group()` を呼び出し、
各ブロックの `variables` リストを結合して返す。

#### 処理フロー

```
section_text
    │
    ├─ _split_survey_blocks() → blocks
    │
    ├─ len(blocks) == 1
    │    → _extract_group(..., group_name="survey") を1回呼び出し
    │       result["variables"] を返す
    │
    └─ len(blocks) >= 2
         for idx, block in enumerate(blocks, start=1):
             result = _extract_group(..., group_name=f"survey_block_{idx}")
             all_variables.extend(result["variables"])
         → all_variables を返す
```

#### コード（抜粋）

```python
def _extract_survey_variables_chunked(self, section_text, instructions):
    blocks = self._split_survey_blocks(section_text)

    if len(blocks) == 1:
        result = self._extract_group(
            section_text=section_text,
            target_fields=["variables"],
            instructions=instructions,
            group_name="survey",
        )
        return result.get("variables") or []

    logger.info("C3: survey text split into %d block(s)", len(blocks))

    all_variables = []
    for idx, block in enumerate(blocks, start=1):
        result = self._extract_group(
            section_text=block,
            target_fields=["variables"],
            instructions=instructions,
            group_name=f"survey_block_{idx}",
        )
        block_vars = result.get("variables")
        if isinstance(block_vars, list):
            all_variables.extend(block_vars)

    logger.info("C3: %d variable(s) extracted from %d block(s)", len(all_variables), len(blocks))
    return all_variables
```

---

### 5. `_run_extraction_groups()` の変更

`survey` グループの処理を `_extract_group()` の直接呼び出しから
`_extract_survey_variables_chunked()` に切り替えた。

```python
# 変更前
partial = self._extract_group(
    section_text=section_text,
    target_fields=group_cfg["fields"],
    instructions=group_cfg["instructions"],
    group_name=group_name,
)
if partial:
    group_results.append(partial)

# 変更後
if group_name == "survey":
    # C3: chunked extraction to handle questionnaires with many variables
    variables = self._extract_survey_variables_chunked(
        section_text, group_cfg["instructions"]
    )
    if variables:
        group_results.append({"variables": variables})
else:
    partial = self._extract_group(...)
    if partial:
        group_results.append(partial)
```

---

### 6. プロンプトの上限記述削除

`EXTRACTION_GROUPS["survey"]["instructions"]` から
100件上限の指示を削除し、全変数を抽出するよう変更した。

```python
# 変更前
"Limit to at most 100 variables. "

# 変更後
"Extract ALL variables present in the provided text — do not skip any. "
```

---

## 動作例

### 標準（小規模調査、分割なし）

```python
agent = PaperToDDIAgent(extractor=extractor)   # vars_per_block=50（デフォルト）

ddi = agent.extract_ddi_from_files({"questionnaire": "questionnaire_30vars.pdf"})
# → _split_survey_blocks() が境界30件 ≤ 50 を検出し、1ブロックで処理
print(len(ddi["variables"]))   # 30
```

### 大規模調査（300変数、6ブロックに分割）

```python
agent = PaperToDDIAgent(extractor=extractor, vars_per_block=50)

ddi = agent.extract_ddi_from_files({"questionnaire": "questionnaire_300vars.pdf"})
# → _split_survey_blocks() が境界300件を検出し、6ブロック（Q1〜50, Q51〜100, ...）に分割
# → _extract_group() を6回呼び出して variables を結合
print(len(ddi["variables"]))   # 最大300（LLM の出力品質に依存）
```

### ブロックサイズのカスタマイズ

```python
# gpt-4o など大きなコンテキストウィンドウを持つモデルでは
# vars_per_block を増やして LLM 呼び出し回数を削減できる
agent = PaperToDDIAgent(extractor=extractor, vars_per_block=100)

# 小さなコンテキストウィンドウのモデルでは減らして精度を上げる
agent = PaperToDDIAgent(extractor=extractor, vars_per_block=30)
```

---

## テスト結果

### `_QUESTION_BOUNDARY_RE` — 13ケース

| テスト内容 | 結果 |
|---|---|
| `Q1.` 英語 Q スタイル | ✅ |
| `Q 12` スペースあり | ✅ |
| `F3.` フォーム変数コード | ✅ |
| `SQ2.` サブ質問コード | ✅ |
| `問1` 日本語スタイル | ✅ |
| `問 5.` スペースあり | ✅ |
| `第1問` 正式日本語スタイル | ✅ |
| `\| Q1 \|` A2 パイプ表形式 | ✅ |
| `\| 問3 \|` A2 パイプ表形式（日本語） | ✅ |
| 一般テキスト → 非マッチ | ✅ |
| `1. 男性  2. 女性`（選択肢行）→ 非マッチ | ✅ |
| `調査票`（ヘッダー）→ 非マッチ | ✅ |

### `_split_survey_blocks()` — 11ケース

| テスト内容 | 結果 |
|---|---|
| 30問 ≤ 50（vars_per_block）→ 1ブロック | ✅ |
| ちょうど50問 → 1ブロック（境界値） | ✅ |
| 120問, vars_per_block=50 → 3ブロック | ✅ |
| 100問, vars_per_block=50 → 2ブロック | ✅ |
| 質問番号なし → 1ブロック（フォールバック） | ✅ |
| ブロック2が Q51 から始まる（境界位置が正確） | ✅ |
| 先頭のプレアンブルテキストがブロック1に含まれる | ✅ |
| vars_per_block=10、25問 → 3ブロック（カスタム設定） | ✅ |
| A2 パイプ表形式、100問 → 2ブロック | ✅ |
| 日本語 問N 形式、60問 → 2ブロック | ✅ |

### `_extract_survey_variables_chunked()` — 4ケース

| テスト内容 | 結果 |
|---|---|
| 1ブロック → `_extract_group` を `group_name="survey"` で1回呼び出し | ✅ |
| 3ブロック → `survey_block_1/2/3` で3回呼び出し・変数を結合（15件） | ✅ |
| 空レスポンスのブロック（variables なし・None）をスキップ | ✅ |
| `vars_per_block` のデフォルト値が 50 | ✅ |

**合計 26/26 PASSED**

---

## 設計上の判断

### 質問境界検出の精度とフォールバック

`_QUESTION_BOUNDARY_RE` は行頭の `Q1.`/`問1`/`第1問`/`| Q1 |` 等を検出するが、
選択肢行（`1. 男性`）や見出し行（`調査票`）は対象外となる設計としてある。
境界が全く検出されない場合（非構造化調査票など）は分割せず全文を1ブロックとして処理し、
従来通りの抽出に自然にフォールバックする。

### プレアンブルの保持

`split_points[0] = 0` としているため、
最初の質問番号より前の文章（調査の説明、記入上の注意など）は
ブロック1の先頭に含まれる。
LLM が質問の文脈を正しく読むのに必要な前文が失われない。

### CV マッピングとの独立性

`_extract_survey_variables_chunked()` は `variables` リストのみを返す。
CV マッピング（`apply_cv_mappings()`）は呼び出し元の
`_run_extraction_groups()` → `extract_ddi_from_text()` /
`extract_ddi_from_files()` で従来通り一括適用される。
ブロック分割が CV マッピングの適用タイミングに影響しない。

### 後方互換性

- `vars_per_block` のデフォルト値 50 のまま、変数数が 50 以下の調査票は
  従来通り 1 回の LLM 呼び出しで処理される。
- `extract_ddi_from_text()` / `extract_ddi_from_file()` の API シグネチャは変更なし。

---

## 関連ドキュメント

| ファイル | 内容 |
|---|---|
| `DDI_PIPELINE_COMPLETE.md` | DDI 生成パイプライン全体の手順と課題（C3 ✅ に更新済み） |
| `DDI_TABLE_EXTRACTION_SUMMARY.md` | A2 表組みレイアウト崩れ対応（パイプ表形式の出力元） |
| `DDI_MULTI_FILE_SUMMARY.md` | D1 複数ファイル一括処理の実装詳細 |
| `DDI_HALLUCINATION_GUARD_SUMMARY.md` | C1 Hallucination 対策の実装詳細 |
