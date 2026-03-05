# DDI 非構造化調査票対応（B3）実装まとめ

## 概要

見出しなしで質問が羅列されている調査票（非構造化調査票）では、
既存の見出し検出ロジック（`_extract_sections()`）が `survey_items` 節を
検出できず、全文が "preamble" 扱いになっていた。

本実装では、`_extract_sections()` の後処理として `_promote_survey_items()` を新規追加した。
preamble テキスト中に質問番号パターン（Q1/問1/第1問/パイプ表形式等）が
`_B3_MIN_QUESTION_COUNT`（デフォルト 3）件以上検出された場合、
preamble を最初の質問境界で自動分割し、質問以降を `survey_items` 節として昇格する（B3）。

これにより、"調査票" や "Questionnaire" などの見出しを持たない調査票PDFでも
変数抽出が正しく動作するようになった。

---

## 変更ファイル一覧

| ファイル | 変更内容 |
|---|---|
| `src/curategpt/agents/paper_to_ddi_agent.py` | `_B3_MIN_QUESTION_COUNT` 定数・`enable_survey_promotion` フィールド・`_promote_survey_items()` 新規・`_extract_sections()` 末尾に昇格呼び出し追加 |
| `tests/agents/test_paper_to_ddi_b3.py` | 新規テストファイル（22ケース） |
| `DDI_PIPELINE_COMPLETE.md` | B3 を ✅ 実装済みに更新 |

---

## 実装詳細

### 1. `_B3_MIN_QUESTION_COUNT` 定数（モジュールレベル）

```python
# 昇格を発動するために preamble 内で検出しなければならない
# 質問境界の最小件数。
# 閾値 > 1 にすることで、「Q1 は重要な設問である」のように
# 質問コードを本文中で言及する報告書での誤検出を防ぐ。
_B3_MIN_QUESTION_COUNT: int = 3
```

---

### 2. `enable_survey_promotion` フィールド（`PaperToDDIAgent` データクラス）

```python
#: When True, the preamble text is scanned for question-number patterns
#: (Q1, F1, SQ1, 問1, 第1問, pipe-table rows) after normal heading-based
#: section detection.  If at least _B3_MIN_QUESTION_COUNT boundaries are
#: found and no survey_items heading was detected, the preamble is split at
#: the first boundary.  Disable if the heuristic causes incorrect splits.
enable_survey_promotion: bool = True
```

`False` に設定すると昇格ロジックを完全に無効化し、従来の節検出のみを使う。
報告書本文中に "Q1 では〜" のような質問コードの言及が多い文書で誤作動する場合に有効。

---

### 3. `_promote_survey_items(sections)` — 昇格メソッド（新規）

`_extract_sections()` が生成したセクションdictを受け取り、
必要に応じて `survey_items` を追加した新しいdictを返す。

#### 処理フロー

```
sections dict
    │
    ├─ "survey_items" が既にある → そのまま返す（見出し優先、昇格なし）
    │
    ├─ preamble が空 → そのまま返す
    │
    ├─ _QUESTION_BOUNDARY_RE.finditer(preamble) → 境界リスト
    │
    ├─ 境界数 < _B3_MIN_QUESTION_COUNT (3)
    │    → そのまま返す（誤検出防止）
    │
    └─ 境界数 ≥ 3
         first_boundary = matches[0].start()
         before = preamble[:first_boundary].strip()
         survey_text = preamble[first_boundary:].strip()
         │
         ├─ before が空でない → preamble = before（表紙テキスト・説明文）
         ├─ before が空 → preamble キーを削除
         └─ survey_items = survey_text（Q1 以降の全テキスト）
```

#### 分割イメージ

```
【入力 preamble】
─────────────────────────────────────────────────────
アンケート調査へのご参加ありがとうございます。
以下の質問にご回答ください。
Q1. あなたの性別を教えてください。
1. 男性  2. 女性  3. その他
Q2. あなたの年齢を教えてください。
（　　）歳
Q3. 現在のご職業は何ですか。
1. 会社員  2. 公務員  3. 自営業
─────────────────────────────────────────────────────

【昇格後】

preamble（bibliographic グループが使用）:
  アンケート調査へのご参加ありがとうございます。
  以下の質問にご回答ください。

survey_items（survey グループが使用）:
  Q1. あなたの性別を教えてください。
  1. 男性  2. 女性  3. その他
  Q2. あなたの年齢を教えてください。
  （　　）歳
  Q3. 現在のご職業は何ですか。
  1. 会社員  2. 公務員  3. 自営業
```

#### コード（抜粋）

```python
def _promote_survey_items(self, sections: Dict[str, str]) -> Dict[str, str]:
    if "survey_items" in sections:
        return sections  # 見出し検出済み → スキップ

    preamble_text = sections.get("preamble", "")
    if not preamble_text:
        return sections

    matches = list(_QUESTION_BOUNDARY_RE.finditer(preamble_text))
    if len(matches) < _B3_MIN_QUESTION_COUNT:
        logger.debug("B3: %d boundary/boundaries (threshold %d) — skipping",
                     len(matches), _B3_MIN_QUESTION_COUNT)
        return sections

    first_boundary = matches[0].start()
    before = preamble_text[:first_boundary].strip()
    survey_text = preamble_text[first_boundary:].strip()

    if not survey_text:
        return sections

    logger.info("B3: Promoted %d chars as survey_items (%d boundaries)",
                len(survey_text), len(matches))

    new_sections = dict(sections)
    if before:
        new_sections["preamble"] = before
    else:
        new_sections.pop("preamble", None)
    new_sections["survey_items"] = survey_text
    return new_sections
```

---

### 4. `_extract_sections()` への呼び出し追加

```python
# 変更前
        result: Dict[str, str] = {"full": text}
        for name, lines in sections.items():
            content = "\n".join(lines).strip()
            if content:
                result[name] = content
        return result

# 変更後
        result: Dict[str, str] = {"full": text}
        for name, lines in sections.items():
            content = "\n".join(lines).strip()
            if content:
                result[name] = content

        # B3: If no survey_items heading was detected, auto-promote question blocks
        if self.enable_survey_promotion:
            result = self._promote_survey_items(result)

        return result
```

---

## 質問境界パターン（`_QUESTION_BOUNDARY_RE` の再利用）

B3 の実装は C3（変数分割処理）で定義した `_QUESTION_BOUNDARY_RE` を共用している。
同じ正規表現が「テキスト分割位置の検出」（C3）と「節の自動昇格」（B3）の両方に使われる。

| パターン | 例 |
|---|---|
| 英語 Q スタイル | `Q1.`, `Q 12`, `q3)` |
| フォーム変数コード | `F1.`, `F 2` |
| サブ質問コード | `SQ1.`, `SQ 3` |
| 日本語 問N | `問1`, `問 2.` |
| 日本語 第N問 | `第1問`, `第 2 問` |
| A2 パイプ表形式 | `\| Q1 \|`, `\| 問3 \|` |

---

## テスト結果

### `_promote_survey_items()` — 14ケース

| テスト内容 | 結果 |
|---|---|
| Q スタイル 3件 → 昇格 | ✅ |
| 問N スタイル 3件 → 昇格 | ✅ |
| F スタイル 3件 → 昇格 | ✅ |
| 第N問 スタイル 3件 → 昇格 | ✅ |
| パイプ表形式 3行 → 昇格 | ✅ |
| 昇格後 survey_items に Q1〜Q3 の内容が含まれる | ✅ |
| 10件（閾値超過）→ 昇格 | ✅ |
| 2件 < 閾値 3 → 昇格しない | ✅ |
| 質問番号なし → 昇格しない | ✅ |
| survey_items が既存 → スキップ（既存値を保持） | ✅ |
| preamble が空 → 昇格しない | ✅ |
| 先頭テキストが preamble に残る・Q1 以降が survey_items に移る | ✅ |
| Q1 がテキスト先頭 → preamble キー削除 | ✅ |
| `full` キーが変化しない | ✅ |
| `_B3_MIN_QUESTION_COUNT` が 3 | ✅ |

### `_extract_sections()` 統合テスト — 7ケース

| テスト内容 | 結果 |
|---|---|
| 見出しなし + 5件 Q スタイル → survey_items 生成 | ✅ |
| 見出しなし + 4件 問N スタイル → survey_items 生成 | ✅ |
| 明示的な「調査票」見出しがある場合は見出し優先 | ✅ |
| `enable_survey_promotion=False` → 昇格なし | ✅ |
| `enable_survey_promotion` デフォルト値が True | ✅ |
| 表紙テキスト + 質問 → 表紙が preamble に残る | ✅ |
| パイプ表形式 4行、見出しなし → survey_items 昇格 | ✅ |

**合計 22/22 PASSED**

---

## 使用方法

### 標準（有効、デフォルト）

```python
agent = PaperToDDIAgent(
    extractor=extractor,
    # enable_survey_promotion=True  ← デフォルトで有効
)

# 見出しなし調査票でも変数を正しく抽出できる
ddi = agent.extract_ddi_from_files({"questionnaire": "questionnaire_no_heading.pdf"})
print(len(ddi["variables"]))   # 全変数が抽出される
```

### 昇格を無効化する場合

```python
# 報告書本文中に「Q1 では〜」のような記述が多く
# survey_items が誤検出される文書では無効化する
agent = PaperToDDIAgent(
    extractor=extractor,
    enable_survey_promotion=False,
)
```

### デバッグ（昇格の確認）

```python
import logging
logging.basicConfig(level=logging.DEBUG)
# ログ出力例:
# INFO paper_to_ddi_agent: B3: Promoted 4521 chars as survey_items
#      (12 question boundaries detected, first at offset 143)
# DEBUG paper_to_ddi_agent: B3: 2 boundary/boundaries in preamble
#       (threshold 3) — skipping survey_items promotion
```

---

## 設計上の判断

### 閾値（`_B3_MIN_QUESTION_COUNT = 3`）の選択

「Q1 は〜」「Q2 については〜」のように、調査設計を説明する報告書本文が
質問コードを言及することがある。このような文書で誤ってpreamble全体を
survey_items に昇格させないよう、最低3件の連続した境界を要求する設計とした。
3件未満の「調査票」（設問数が少ない場合）は全文フォールバックで引き続き
正常に動作するため、実用上の問題はない。

### 既存見出し検出との優先関係

`_extract_sections()` の見出し検出（"調査票", "Questionnaire" 等）が
`_promote_survey_items()` より先に実行される。
見出しで `survey_items` が既に確立されている場合、昇格メソッドは即座に
early-return するため、見出し付き文書の動作は変わらない。

### preamble の保持

最初の質問境界より前のテキスト（調査目的・記入上の注意等）は preamble に残す。
bibliographic グループがこの preamble から調査概要・実施機関等を抽出するため、
この前文が失われないことが重要である。

### `full` テキストの不変性

昇格処理は sections dict の preamble キーと survey_items キーのみを変更する。
`full` キー（元の完全テキスト）は変更しない。
これにより、いずれかのグループが全文フォールバック（`full` テキスト）を
使う場合も正しく動作することが保証される。

### `_QUESTION_BOUNDARY_RE` の共用（C3 との関係）

C3（変数件数上限対応）で定義した `_QUESTION_BOUNDARY_RE` を B3 でも共用している。
同一の正規表現が次の2つの目的に使われる：

- **C3**: survey_items テキストをブロックに分割する位置の検出
- **B3**: preamble 内で survey_items に昇格すべき開始位置の検出

パターンの変更は両方の動作に影響するため、変更する場合は両方のテストを実行すること。

---

## 関連ドキュメント

| ファイル | 内容 |
|---|---|
| `DDI_PIPELINE_COMPLETE.md` | DDI 生成パイプライン全体の手順と課題（B3 ✅ に更新済み） |
| `DDI_CHUNKED_SURVEY_SUMMARY.md` | C3 変数件数上限対応（`_QUESTION_BOUNDARY_RE` の定義元） |
| `DDI_TABLE_EXTRACTION_SUMMARY.md` | A2 表組みレイアウト崩れ対応（パイプ表形式の出力元） |
| `DDI_HALLUCINATION_GUARD_SUMMARY.md` | C1 Hallucination 対策の実装詳細 |
