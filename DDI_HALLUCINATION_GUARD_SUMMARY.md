# C1 Hallucination 対策 実装サマリー

本ドキュメントは、`PaperToDDIAgent` に追加した **Hallucination（幻覚）対策（C1）** の実装内容を記録したものです。

---

## 1. 問題の概要

`PaperToDDIAgent` が LLM（GPT-4o 等）を呼び出してDDIメタデータを抽出する際、
文書に**明記されていないフィールド**を LLM が推測・生成してしまう問題があった。

### 頻発フィールドと発生パターン

| フィールド | 典型的な幻覚パターン |
|---|---|
| `study_ids` | アーカイブ識別子（`ZA1234`, `UKDA-6789` 等）が存在しないのに捏造される |
| `funding_agencies` | 著者の所属機関や一般的な公的機関名が推測で生成される |
| `collection_dates` | 出版年・著作権年・曖昧な期間記述から日付が導出・変換される |
| `distributors` | アーカイブ機関名が根拠なく挿入される |
| `alternative_title` | 副題が存在しないのに英訳や短縮形が生成される |

### 改修前の状態

各グループの `instructions` に
`"Return null for any field that cannot be determined from the text."` という指示はあったが、
LLM が「決定できない」と判断する閾値が高く、推測が止まらない状態だった。

---

## 2. 実装方針

3層（L1〜L3）の防衛を重ねて実装した。

```
【LLM 呼び出し前】
  L1: _HALLUCINATION_GUARD を全グループのプロンプト冒頭に挿入（強制ルール宣言）
  L2: EXTRACTION_GROUPS の instructions に高リスクフィールド個別警告を追加

【LLM 呼び出し後】
  L3: _check_against_source() で抽出値を原文と照合 → 存在しない値を警告＋自動 null 化
```

---

## 3. 変更ファイル

| ファイル | 変更種別 |
|---|---|
| `src/curategpt/agents/paper_to_ddi_agent.py` | 改修（定数追加・メソッド追加・既存コード変更） |
| `DDI_PIPELINE_COMPLETE.md` | C1 を ✅ 実装済みに更新 |

---

## 4. 実装詳細

### 4.1 L1: `_HALLUCINATION_GUARD` 定数（プロンプト強化）

**ファイル位置**: `paper_to_ddi_agent.py` L240–249（`# Hallucination countermeasures (C1)` ブロック）

```python
_HALLUCINATION_GUARD = """\
EXTRACTION RULES — mandatory, no exceptions:
1. Extract ONLY information that is EXPLICITLY present in the text provided below.
2. Do NOT use your general world knowledge, make inferences, or generate
   plausible-sounding values for missing information.
3. Do NOT invent identifiers, dates, personal names, or institution names
   that do not appear verbatim (or near-verbatim) in the text.
4. If a field is not clearly stated in the text, output null — never guess.
5. When in doubt, prefer null over an uncertain value.
"""
```

**挿入箇所**: `_extract_group()` のプロンプト組み立て部（`prompt_parts.append(...)` の先頭）

```python
# 改修前
prompt_parts.append(
    f"You are a social-science metadata expert. {instructions}\n"
    f"Return ONLY valid YAML — no markdown fences, no prose.\n"
    f"Fields to extract: {', '.join(target_fields)}\n"
)

# 改修後
prompt_parts.append(
    f"You are a social-science metadata expert.\n"
    f"\n"
    f"{_HALLUCINATION_GUARD}\n"       # ← ここに挿入
    f"{instructions}\n"
    f"Return ONLY valid YAML — no markdown fences, no prose.\n"
    f"Fields to extract: {', '.join(target_fields)}\n"
)
```

**効果**: 3グループ（`bibliographic` / `methodology` / `survey`）すべての LLM 呼び出しに
強制ルール宣言が付加される。

---

### 4.2 L2: `EXTRACTION_GROUPS` per-field 警告

**ファイル位置**: `paper_to_ddi_agent.py` `EXTRACTION_GROUPS` 定数内 `"instructions"` キー

#### `bibliographic` グループへの追加

```python
# 改修前
"Return null for any field that cannot be determined from the text."

# 改修後（追加部分を太字で示す）
"IMPORTANT — 'study_ids': must be an official archive identifier "
"(e.g., ZA1234, UKDA-6789, e-Stat:00001) EXPLICITLY PRINTED in the document. "
"Do NOT invent one if none is present — return null. "
"IMPORTANT — 'funding_agencies': must be named in a funding statement or "
"acknowledgement section. Do NOT infer from institutional affiliation "
"or guess from general knowledge — return null if absent. "
"Return null for any other field that cannot be determined from the text."
```

#### `methodology` グループへの追加

```python
# 改修前
"'collection_mode' describes how data was collected ..."
"Return null for any field that cannot be determined."

# 改修後（追加部分を太字で示す）
"IMPORTANT — 'collection_dates': use ONLY dates explicitly stated in the text. "
"Do NOT derive dates from the publication year, copyright year, or vague "
"period descriptions — return null if no concrete dates are present. "
"'collection_mode' describes how data was collected ..."
"Return null for any field that cannot be determined."
```

**高リスクフィールドと警告の対応**

| フィールド | グループ | 追加された個別指示 |
|---|---|---|
| `study_ids` | `bibliographic` | 公式アーカイブ識別子のみ。捏造禁止 |
| `funding_agencies` | `bibliographic` | 資金提供声明・謝辞に明記されている場合のみ。所属推測禁止 |
| `collection_dates` | `methodology` | 文書中に明示された日付のみ。出版年・著作権年からの導出禁止 |

---

### 4.3 L3: `_check_against_source()` メソッド（原文照合チェック）

**ファイル位置**: `paper_to_ddi_agent.py` L587–665（`# Hallucination source-check (C1)` ブロック）

#### チェック対象フィールド

```python
_SOURCE_CHECK_FIELDS: frozenset = frozenset(
    {
        "study_ids",
        "funding_agencies",
        "distributors",
        "alternative_title",
    }
)
```

**除外フィールド（チェックしない）**

| フィールド | 除外理由 |
|---|---|
| `title` | LLM が整形・要約することが多く過検出になりやすい |
| `principal_investigators` | 姓名の順序・空白が揺れることがある |
| `variables` | list-of-dict 型。複数テキスト断片から組み立てるため逐語一致は期待できない |
| `time_periods`, `collection_dates` | dict 型フィールド。照合不適 |
| `keywords`, `topic_classification` | 意味的抽象化が多い |

#### 照合ロジック

```python
def _check_against_source(self, extracted, source_text):
    # 原文を1回だけ正規化（効率化）
    source_norm = re.sub(r"\s+", " ", source_text).lower()

    def _found(value: str) -> bool:
        needle = re.sub(r"\s+", " ", value.strip()).lower()
        return bool(needle) and needle in source_norm

    cleaned = dict(extracted)

    for field in _SOURCE_CHECK_FIELDS:
        val = cleaned.get(field)
        if val is None:
            continue

        if isinstance(val, str):          # スカラー文字列
            if not _found(val):
                logger.warning(...)
                cleaned[field] = None     # null 化

        elif isinstance(val, list):       # リスト
            kept = []
            for item in val:
                if isinstance(item, str):
                    if _found(item):
                        kept.append(item) # 存在 → 残す
                    else:
                        logger.warning(...) # 不在 → 除去
                else:
                    kept.append(item)     # dict 型 → スキップ
            cleaned[field] = kept if kept else None

    return cleaned
```

**照合方式**

| 処理 | 内容 |
|---|---|
| 大文字小文字 | 無視（`.lower()`） |
| 空白の揺れ | 正規化（連続空白を1スペースに統一） |
| マッチング | substring 検索（`in` 演算子）。部分一致で OK |
| dict 型アイテム | チェック対象外（そのまま残す） |

#### `extract_ddi_from_text()` への呼び出し追加

```python
merged = self._merge_extractions(group_results)

# L3: cross-check extracted values against the source text
if self.enable_source_check:           # ← 追加
    merged = self._check_against_source(merged, text)

# Fallback: use filename as title when not extracted
if not merged.get("title") and filename:
    merged["title"] = Path(filename).stem
```

#### `enable_source_check` パラメータ

```python
@dataclass
class PaperToDDIAgent(BaseAgent):
    context_tokens: int = _DEFAULT_CONTEXT_TOKENS
    rag_example_limit: int = 3
    enable_source_check: bool = True    # ← 追加
```

チェックが厳しすぎる場合（機関名の表記揺れが激しい文書など）は
`PaperToDDIAgent(enable_source_check=False)` で無効化できる。

---

## 5. プロンプト構造の変化（改修前後の比較）

### 改修前

```
You are a social-science metadata expert. {instructions}
Return ONLY valid YAML — no markdown fences, no prose.
Fields to extract: title, abstract, ...

--- Example outputs ---
...
--- End examples ---

Text to analyse:
=== ABSTRACT ===
...

YAML output:
```

### 改修後

```
You are a social-science metadata expert.

EXTRACTION RULES — mandatory, no exceptions:        ← L1 追加
1. Extract ONLY information EXPLICITLY present ...
2. Do NOT use general world knowledge ...
3. Do NOT invent identifiers ...
4. If a field is absent, output null — never guess.
5. When in doubt, prefer null.

{instructions}                                        ← L2 個別警告を含む
  ...IMPORTANT — 'study_ids': must be an official archive identifier...
  ...IMPORTANT — 'funding_agencies': named in funding statement only...
Return ONLY valid YAML — no markdown fences, no prose.
Fields to extract: title, abstract, ...

--- Example outputs ---
...
--- End examples ---

Text to analyse:
=== ABSTRACT ===
...

YAML output:
```

---

## 6. テスト結果

`_check_against_source()` のロジックを直接スモークテスト（Python スクリプト）で検証。

**テスト原文**:
```
本調査は国立社会保障・人口問題研究所が実施した。
調査番号: IPSS-2023-01。資金提供機関: 厚生労働省。
調査期間: 2023年10月〜2023年12月。
```

| # | テストケース | 入力値 | 期待結果 | 実際の結果 |
|---|---|---|---|---|
| 1 | study_id 原文にあり | `["IPSS-2023-01"]` | `["IPSS-2023-01"]` | ✓ |
| 2 | study_id 原文になし | `["ZA9999"]` | `None` | ✓ |
| 3 | funding_agency あり | `["厚生労働省"]` | `["厚生労働省"]` | ✓ |
| 4 | funding_agency なし | `["文部科学省"]` | `None` | ✓ |
| 5 | リスト混在（有効1件+幻覚1件） | `["IPSS-2023-01","ZA9999"]` | `["IPSS-2023-01"]` | ✓ |
| 6 | 非対象フィールド(title) | `"架空タイトル"` | `"架空タイトル"` | ✓ |
| 7 | distributor 幻覚 | `["ICPSR"]` | `None` | ✓ |
| 8 | None パススルー | `None` | `None` | ✓ |

**結果: 8/8 パス**

---

## 7. 各層の効果と限界

| 層 | 効果 | 限界 |
|---|---|---|
| **L1 プロンプト強化** | LLM が「推測してよい」と判断する機会を減らす。最も広範囲に作用する | LLM がルールを無視することはゼロにはならない |
| **L2 フィールド別警告** | 特定フィールドに対して具体的な禁止事例を示すことで遵守率が上がる | 全フィールドをカバーしているわけではない |
| **L3 原文照合チェック** | LLM が幻覚値を出力した場合でも確実に検出・除去できる | 表記揺れ・略称・翻訳値を誤検出する可能性がある |

### L3 の誤検出リスクと対策

| 状況 | リスク | 対策 |
|---|---|---|
| 機関名の略称（`MHLW` ← `厚生労働省`） | 略称が原文にない場合に誤検出 | WARNING ログを確認し手動復元 |
| 英語文書で日本語機関名が正規化されない | 文字種変換後の値が不一致 | `enable_source_check=False` で無効化 |
| `study_ids` が表紙ではなく奥付に記載 | FullDocumentWrapper で読み込めていれば問題なし | 全文読み込みを必ず使用する |

---

## 8. 使用方法

### 標準（source check 有効）

```python
from curategpt.agents.paper_to_ddi_agent import PaperToDDIAgent

agent = PaperToDDIAgent(
    knowledge_source=db,
    extractor=extractor,
    enable_source_check=True,   # デフォルト
)
ddi = agent.extract_ddi_from_file("survey_report.pdf")
# WARNING ログに "Hallucination guard [...]" が出たフィールドは手動確認推奨
```

### source check 無効（表記揺れが激しい文書向け）

```python
agent = PaperToDDIAgent(
    knowledge_source=db,
    extractor=extractor,
    enable_source_check=False,  # L3 のみ無効化。L1・L2 は常に有効
)
```

### WARNING ログの確認方法

```python
import logging
logging.basicConfig(level=logging.WARNING)

# 出力例:
# WARNING:paper_to_ddi_agent:Hallucination guard [study_ids]:
#   'ZA9999' not found in source text — setting to null.
#   Review and restore manually if incorrect.
```

---

## 9. 今後の改善候補

| # | 内容 | 工数 | 優先度 |
|---|---|---|---|
| H1 | `title` の有意語（長さ4字以上の語）照合による緩いチェック追加 | 小 | 中 |
| H2 | `principal_investigators` の姓のみ照合（名前全体の一致を要求しない） | 小 | 中 |
| H3 | 抽出値と原文の一致箇所を `_evidence` フィールドとして返すオプション | 中 | 低 |
| H4 | 照合結果を HTML レポートとして出力し、人手確認を支援する | 大 | 低 |
