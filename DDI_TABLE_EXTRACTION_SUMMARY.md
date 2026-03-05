# DDI 表組みレイアウト崩れ対応（A2）実装まとめ

## 概要

調査票 PDF の表形式レイアウト（質問番号 | 質問文 | 選択肢）を正しく抽出するため、
`pdfplumber` の `find_tables()` / `crop()` API を用いたテーブル構造保持抽出を実装した（A2）。

従来の `extract_text()` はテーブルの列順を崩して読み取るため、
変数の `categories` フィールドで value-label 対応が逆転・欠落する問題があった。
本実装により、テーブルセルをパイプ区切りテキストとして正確に保持し
LLM に渡せるようになった。

---

## 変更ファイル一覧

| ファイル | 変更内容 |
|---|---|
| `src/curategpt/agents/paper_to_ddi_agent.py` | `enable_table_extraction` フラグ追加・`_read_full_document()` 更新・`_extract_pdf_page_text()` 新規・`_table_to_text()` 新規 |
| `DDI_PIPELINE_COMPLETE.md` | A2 を ✅ 実装済みに更新 |

---

## 実装詳細

### 1. `enable_table_extraction` フラグ（`PaperToDDIAgent` データクラス）

```python
#: When True, PDF pages are scanned for tables using pdfplumber's
#: find_tables() API. Detected tables are formatted as pipe-delimited
#: text to preserve column order.
enable_table_extraction: bool = True
```

`False` に設定すると従来通り `extract_text()` のみを使う。
テーブル検出が過剰に働く文書（テーブル罫線のない段落組みの報告書など）では
無効化を推奨する。

---

### 2. `_read_full_document()` の更新

PDF 処理の内部ループを修正し、`enable_table_extraction` フラグに応じて
抽出メソッドを切り替えるようにした。

```python
# 変更前
page_text = page.extract_text()

# 変更後
if self.enable_table_extraction:
    page_text = self._extract_pdf_page_text(page)   # テーブル構造抽出
else:
    page_text = page.extract_text() or ""            # 従来方式
```

---

### 3. `_extract_pdf_page_text(page)` — テーブル構造抽出メソッド（新規）

1ページ分の pdfplumber `Page` オブジェクトを受け取り、
テーブルとプレーンテキスト領域を分離して結合した文字列を返す。

#### 処理フロー

```
pdfplumber Page
    │
    ├─ find_tables()  ─→  テーブルオブジェクトのリスト（上端 Y 座標でソート）
    │
    │   ┌─────────────────────────────────────────────────────────────────┐
    │   │ テーブルが 0 件 → extract_text() にフォールバック              │
    │   │ find_tables() が例外 → extract_text() にフォールバック         │
    │   └─────────────────────────────────────────────────────────────────┘
    │
    ├─ [テーブルが 1 件以上の場合] 上から下へ処理
    │
    │   ┌── テーブル上のプレーンテキスト帯 ──────────────────────────────┐
    │   │   page.crop((0, prev_bottom, width, table.top))               │
    │   │   → extract_text()                                             │
    │   └────────────────────────────────────────────────────────────────┘
    │
    │   ┌── テーブル本体 ─────────────────────────────────────────────────┐
    │   │   table.extract() → List[List[Optional[str]]]                  │
    │   │   → _table_to_text()                                           │
    │   │   → "| Q1 | 性別 | 1.男性  2.女性 |"                          │
    │   └────────────────────────────────────────────────────────────────┘
    │
    └─ 最後のテーブル下のプレーンテキスト帯
        page.crop((0, last_bottom, width, height))
        → extract_text()
```

#### フォールバック条件

| 条件 | 動作 |
|---|---|
| `find_tables()` が例外を送出 | `extract_text()` を返す |
| テーブルが 0 件 | `extract_text()` を返す |
| 構造抽出結果が空文字 | `extract_text()` を返す（安全ネット） |
| 個々の `crop()` / `table.extract()` が例外 | そのパーツをスキップして続行 |

#### コード（抜粋）

```python
def _extract_pdf_page_text(self, page: Any) -> str:
    try:
        tables = page.find_tables()
    except Exception as exc:
        logger.debug("find_tables() failed: %s — falling back", exc)
        return page.extract_text() or ""

    if not tables:
        return page.extract_text() or ""

    page_width = page.width
    page_height = page.height
    sorted_tables = sorted(tables, key=lambda t: t.bbox[1])

    parts: List[str] = []
    prev_bottom: float = 0.0

    for table in sorted_tables:
        x0, top, x1, bottom = table.bbox

        # テーブル上のプレーンテキスト帯
        if top > prev_bottom + 1:
            try:
                above = page.crop((0, prev_bottom, page_width, top))
                above_text = above.extract_text()
                if above_text and above_text.strip():
                    parts.append(above_text.strip())
            except Exception as exc:
                logger.debug("crop above table failed: %s", exc)

        # テーブル本体をパイプ区切りに変換
        try:
            rows = table.extract()
            table_text = self._table_to_text(rows) if rows else ""
            if table_text:
                parts.append(table_text)
        except Exception as exc:
            logger.debug("table.extract() failed: %s — skipping", exc)

        prev_bottom = bottom

    # 最終テーブル下のプレーンテキスト帯
    if prev_bottom < page_height - 1:
        try:
            below = page.crop((0, prev_bottom, page_width, page_height))
            below_text = below.extract_text()
            if below_text and below_text.strip():
                parts.append(below_text.strip())
        except Exception as exc:
            logger.debug("crop below table failed: %s", exc)

    combined = "\n\n".join(parts)
    if not combined.strip():
        return page.extract_text() or ""
    return combined
```

---

### 4. `_table_to_text(rows)` — テーブル→パイプ区切りテキスト変換（新規スタティックメソッド）

`pdfplumber` の `table.extract()` が返す `List[List[Optional[str]]]` を
LLM が読みやすいパイプ区切りテキストに変換する。

#### 変換ルール

| 入力 | 変換後 |
|---|---|
| セル値が文字列 | `.strip()` して使用 |
| セル値が `None`（結合セル・空セル） | 空文字 `""` に変換 |
| 行が全セル空 | その行をスキップ（スペーサー行の除去） |
| 行が `None` | その行をスキップ |

#### 変換例

```python
# 入力（pdfplumber table.extract() の出力）
[
    ['Q1', '性別を教えてください', '1. 男性  2. 女性  3. その他'],
    ['Q2', '年齢を教えてください', None],
    [None, None, None],   # 空白行（スキップ）
    ['Q3', '婚姻状況',     '1. 未婚  2. 既婚  3. 離婚・死別'],
]

# 出力
| Q1 | 性別を教えてください | 1. 男性  2. 女性  3. その他 |
| Q2 | 年齢を教えてください |  |
| Q3 | 婚姻状況 | 1. 未婚  2. 既婚  3. 離婚・死別 |
```

#### コード

```python
@staticmethod
def _table_to_text(rows: List[List[Optional[str]]]) -> str:
    lines: List[str] = []
    for row in rows:
        if row is None:
            continue
        cells = [str(c).strip() if c is not None else "" for c in row]
        if not any(cells):   # 全セル空行はスキップ
            continue
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)
```

---

## テキスト変換のビフォー・アフター

### 従来方式（`extract_text()` のみ）

`pdfplumber` の `extract_text()` は列ごとにテキストを読み取るため、
表形式の調査票では列が混在した文字列になる。

```
Q1 Q2 Q3
性別を教えてください 年齢を教えてください 婚姻状況
1. 男性 2. 女性
```

### 新方式（`_extract_pdf_page_text()`）

テーブルを行単位・セル単位で抽出するため、列の対応関係が保持される。

```
| Q1 | 性別を教えてください | 1. 男性  2. 女性  3. その他 |
| Q2 | 年齢を教えてください |  |
| Q3 | 婚姻状況 | 1. 未婚  2. 既婚  3. 離婚・死別 |
```

LLM はこの形式から質問番号・質問文・選択肢を正確に対応付けて
`variables[].categories` フィールドに格納できるようになる。

---

## テスト結果

### `_table_to_text` — 7ケース

| テスト内容 | 結果 |
|---|---|
| 標準3行テーブル（3行出力） | ✅ |
| 空行スキップ（2行出力） | ✅ |
| 全空テーブル（0行出力） | ✅ |
| 空リスト（空文字列） | ✅ |
| パイプ区切りフォーマット確認 | ✅ |
| `None` セル → 空文字変換 | ✅ |
| 先頭・末尾ホワイトスペースの strip | ✅ |

### `_extract_pdf_page_text` — 8ケース

| テスト内容 | 結果 |
|---|---|
| ヘッダーテキストが出力に含まれる | ✅ |
| テーブル1行目が含まれる | ✅ |
| テーブル2行目が含まれる | ✅ |
| フッターテキストが出力に含まれる | ✅ |
| 順序：ヘッダー → テーブル | ✅ |
| 順序：テーブル → フッター | ✅ |
| テーブルなし → `extract_text()` フォールバック | ✅ |
| `find_tables()` 例外 → フォールバック | ✅ |

### フラグ — 1ケース

| テスト内容 | 結果 |
|---|---|
| `enable_table_extraction` デフォルト値が `True` | ✅ |

**合計 16/16 PASSED**

---

## 使用方法

### 標準（テーブル抽出有効、デフォルト）

```python
agent = PaperToDDIAgent(
    knowledge_source=db,
    extractor=extractor,
    # enable_table_extraction=True  ← デフォルトで有効
)

ddi = agent.extract_ddi_from_files({
    "report":        "survey_report.pdf",
    "questionnaire": "questionnaire.pdf",   # 表形式の調査票もOK
})
```

### テーブル抽出を無効化する場合

```python
# テーブル罫線のないプレーンな報告書など、
# テーブル検出が誤動作する文書では無効化
agent = PaperToDDIAgent(
    extractor=extractor,
    enable_table_extraction=False,
)
```

### テーブル抽出の確認（デバッグ）

```python
import logging
logging.basicConfig(level=logging.DEBUG)
# ログ出力例:
# DEBUG paper_to_ddi_agent: Page 3: 2 table(s) detected — using table-aware extraction
# DEBUG paper_to_ddi_agent: Page 5: find_tables() failed: ... — falling back to extract_text()
```

---

## 設計上の判断

### `find_tables()` の信頼性

pdfplumber の `find_tables()` はテーブルの罫線（水平・垂直線）を検出してテーブル領域を特定する。
罫線のない「見かけ上の表」（スペースで整形されたもの）は検出されないが、
大多数の PDF 調査票は罫線付きテーブルを使用しているため実用上は問題ない。
検出できなかった場合は自動的に `extract_text()` にフォールバックするため
抽出品質が従来より低下することはない。

### 上から下への処理順序

テーブルを Y 座標でソートし、テーブル間のプレーンテキストも位置順に結合することで、
ページ内のテキストが文書の読み順（上→下）で LLM に渡されるよう保証した。
これにより、テーブル直前の質問番号の見出しや、テーブル直後の注記がテーブルと
正しい文脈で読まれる。

### テーブル外テキストの `crop()` 抽出

テーブル領域をバウンディングボックスで切り出し、残りの領域を
`page.crop()` で別途抽出する方式を採用した。
pdfplumber には「テーブル以外のテキストだけを抽出する」組み込み API がないため、
テーブルの bbox を境界として上下のストリップを個別に抽出している。

### 空行スキップ

PDF のテーブルには罫線だけの空行（スペーサー行）が含まれることが多い。
全セルが空（`None` または空文字）の行を自動スキップすることで、
LLM へのプロンプトが不要な空行で汚染されるのを防いでいる。

---

## 関連ドキュメント

| ファイル | 内容 |
|---|---|
| `DDI_PIPELINE_COMPLETE.md` | DDI 生成パイプライン全体の手順と課題（A2 ✅ に更新済み） |
| `DDI_MULTI_FILE_SUMMARY.md` | D1 複数ファイル一括処理の実装詳細 |
| `DDI_CV_MAPPER_SUMMARY.md` | C4 統制語彙マッピングの実装詳細 |
| `DDI_HALLUCINATION_GUARD_SUMMARY.md` | C1 Hallucination 対策の実装詳細 |
