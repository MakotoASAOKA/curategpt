# 調査票・調査報告書から DDI-Codebook メタデータを生成する方法と課題

本ドキュメントは、CurateGPT に追加実装した DDI パイプラインを用いて、
社会調査の **調査票（questionnaire）** および **調査報告書（survey report）** から
**DDI-Codebook 2.5** メタデータを生成するための、
現状の実装状況・手順・残課題を体系的に整理したものです。

---

## 1. 実装済みコンポーネント一覧

今回の開発で追加したファイルをまとめる。

| コンポーネント | ファイル | 役割 |
|---|---|---|
| **DDI事例収集** | `wrappers/social/ddi_codebook_wrapper.py` | OAI-PMH で国際DDIリポジトリを収集し ChromaDB に格納。RAG の知識ベースを構築する |
| **全文読み込み** | `wrappers/social/full_document_wrapper.py` | PDF/DOCX を3,000文字チャンク分割せず1件のレコードとして読み込む |
| **メタデータ抽出** | `agents/paper_to_ddi_agent.py` | 論文・調査票を節単位に分割し、グループごとにLLMを呼び出してDDIフィールドを抽出する |
| **XML出力** | `wrappers/social/ddi_xml_serializer.py` | Python dict を DDI-Codebook 2.5 準拠 XML に変換する |

### パイプライン全体像

```
【入力文書】                【処理レイヤー】                          【出力】

調査報告書 (PDF)  ──┐
                    │  FullDocumentWrapper                        DDI dict
調査票 (PDF)      ──┼─► （チャンクなし全文読み込み）
                    │         │
                    │         ▼
                    │  PaperToDDIAgent
                    │  ┌──────────────────────────────┐
                    │  │ Pass 1: 節検出（正規表現）      │
                    │  │ Pass 2: グループ別LLM抽出      │◄── ChromaDB（RAG事例）
                    │  │   - bibliographic グループ     │        ↑
                    │  │   - methodology グループ       │  DDICodebookWrapper
                    │  │   - survey グループ            │  （OAI-PMH で事例収集）
                    │  └──────────────────────────────┘
                    │         │
                    │         ▼
                    └──► DDI metadata dict
                                │
                                ▼
                         DDIXMLSerializer
                                │
                                ▼
                         DDI-Codebook 2.5 XML  ──► output.xml
```

---

## 2. 前提条件

### 必要な依存ライブラリ

```bash
# 基本（pyproject.toml に含まれる）
pip install curategpt

# PDF 抽出（推奨）
pip install pdfplumber

# バイナリ形式（DOCX等）の抽出
pip install textract    # OS 依存。Windows では追加作業が必要な場合あり

# LLM（OpenAI）
pip install llm
llm install llm-openai
llm keys set openai   # または環境変数 OPENAI_API_KEY を設定
```

| ライブラリ | 用途 | 必須度 |
|---|---|---|
| `pdfplumber` | PDF テキスト抽出（レイアウト保持） | 強く推奨 |
| `textract` | DOCX / XLS / PPT 等のテキスト抽出 | PDF のみなら不要 |
| `llm` + `llm-openai` | LLM によるメタデータ抽出 | 必須 |
| `xmltodict` | OAI-PMH XML の解析 | pyproject.toml に含まれる |

### 動作確認済み環境

| 項目 | 値 |
|---|---|
| Python | 3.11 以上 |
| 推奨 LLM | `gpt-4o`（128k コンテキスト。長文調査報告書に対応） |
| 代替 LLM | `gpt-4`（8k コンテキスト。短いテキストのみ） |

---

## 3. 手順

### Step 1：DDI 事例の収集（ナレッジベース構築）

`PaperToDDIAgent` はRAGにより既存のDDI事例をプロンプトに付加する。
まず国際リポジトリから DDI 事例を ChromaDB に収集する。

#### CLI で実行

```bash
# GESIS（欧州社会科学、英語・独語中心）
curategpt view index -c ddi_examples -m openai: --view ddi_codebook \
    --init-with "{oai_base_url: 'https://api.gesis.org/DBK/OAI-PMH', max_records: 200}"

# SSJDA（日本社会科学データアーカイブ、日本語対応）
curategpt view index -c ddi_examples -m openai: --view ddi_codebook \
    --init-with "{oai_base_url: 'https://ssjda.iss.u-tokyo.ac.jp/oai', max_records: 200}"
```

#### Python で実行

```python
from curategpt.store import ChromaDBAdapter
from curategpt.wrappers.social.ddi_codebook_wrapper import (
    DDICodebookWrapper, GESIS_OAI_URL, SSJDA_OAI_URL
)

db = ChromaDBAdapter("~/.curategpt/db")

# GESIS から収集
wrapper = DDICodebookWrapper(oai_base_url=GESIS_OAI_URL, max_records=200)
db.upsert(list(wrapper.objects()), collection="ddi_examples", model="openai:")
```

> **cold-start でも動作する：** `knowledge_source=None` にすれば RAG なしで抽出可能。ただし
> 事例がある場合と比べて出力品質が低下するため、最低50件の収集を推奨。

---

### Step 2：入力ファイルの確認

| ファイル種別 | 推奨形式 | 注意点 |
|---|---|---|
| 調査報告書 | テキスト埋め込み PDF | pdfplumber で直接読み込み可 |
| 調査票 | テキスト埋め込み PDF | 表組みのレイアウト崩れに注意（→後述の課題 A2） |
| スキャン PDF | OCR 変換後の PDF | textract + tesseract が必要。日本語精度は環境依存 |
| DOCX | Word 形式 | textract で読み込み可 |

---

### Step 3：メタデータ抽出

#### Python スクリプト（推奨）

```python
from curategpt.store import ChromaDBAdapter
from curategpt.extract.basic_extractor import BasicExtractor
from curategpt.agents.paper_to_ddi_agent import PaperToDDIAgent
import llm

# 準備
db = ChromaDBAdapter("~/.curategpt/db")
extractor = BasicExtractor()
extractor.model = llm.get_model("gpt-4o")

agent = PaperToDDIAgent(
    knowledge_source=db,
    knowledge_source_collection="ddi_examples",   # Step 1 で収集した事例
    extractor=extractor,
    context_tokens=128_000,    # gpt-4o の場合
    rag_example_limit=3,       # プロンプトに付加する事例数
    enable_source_check=True,  # Hallucination 原文照合チェック（デフォルト有効）
)

# ─── 【推奨】複数ファイルを一括処理（D1 実装済み）───
ddi_merged = agent.extract_ddi_from_files({
    "report":        "survey_report.pdf",   # 書誌・方法論を抽出
    "questionnaire": "questionnaire.pdf",   # 変数情報を抽出
})

# ─── 【参考】単一ファイル処理（従来方式）───
# ddi_merged = agent.extract_ddi_from_file("survey_report.pdf")

# ─── 複数ファイル同一ロール（例：複数年次調査）───
# ddi_merged = agent.extract_ddi_from_files([
#     ("report",        "report_2022.pdf"),
#     ("report",        "report_2023.pdf"),
#     ("questionnaire", "questionnaire.pdf"),
# ])
```

#### 抽出される主要フィールド

| グループ | フィールド名 | 抽出元 |
|---|---|---|
| **書誌** | `title`, `alternative_title`, `principal_investigators`, `funding_agencies`, `abstract`, `keywords`, `topic_classification` | 調査報告書の表紙・概要・序章 |
| **調査方法** | `time_periods`, `collection_dates`, `nations`, `geographic_coverage`, `universe`, `analysis_unit`, `sampling_procedure`, `collection_mode`, `time_method` | 調査報告書の方法節・対象者節 |
| **変数** | `variables`（name / label / question / filter_condition / categories / format を各変数で抽出） | 調査票の質問項目 |

---

### Step 4：結果の確認

```python
import json

# ─── JSON で内容確認 ───
print(json.dumps(ddi_merged, ensure_ascii=False, indent=2))

# ─── 主要フィールドのみ確認 ───
check_fields = [
    "title", "principal_investigators",
    "universe", "sampling_procedure", "collection_mode"
]
for f in check_fields:
    print(f"{f}: {ddi_merged.get(f, '（未取得）')}")

# ─── 変数情報の確認 ───
vars_ = ddi_merged.get("variables", [])
print(f"\n変数数: {len(vars_)}")
for v in vars_[:5]:    # 先頭5件を確認
    print(f"  {v.get('name', '?')} — {v.get('label', '?')}")
```

#### 確認チェックリスト

```
[ ] title          — 正式な調査名称と一致しているか
[ ] principal_investigators — 氏名の表記（姓名順・ふりがな等）が正しいか
[ ] time_periods   — 調査実施期間が正確か（ISO 8601 形式: YYYY-MM-DD）
[ ] universe       — 調査母集団の定義が正確か
[ ] sampling_procedure — 標本抽出方法が正確か
[ ] collection_mode — 調査モードが正しいか（「自計式」「他計式」等）
[ ] variables      — 変数名・質問文・選択肢コードが正確か
[ ] filter_condition — フィルター条件の有無と内容が正しいか（フィルターなしの設問は null）
[ ] 統制語彙       — topic_classification・collection_mode が統制語を使っているか
```

---

### Step 5：DDI XML 出力

```python
from curategpt.wrappers.social.ddi_xml_serializer import DDIXMLSerializer, to_ddi_xml

s = DDIXMLSerializer()

# ─── 構造検証（出力前の推奨手順）───
ok, errors = s.validate_structure(ddi_merged)
if not ok:
    print("検証エラー・警告：")
    for e in errors:
        print(f"  {e}")

# ─── XML ファイルとして保存 ───
s.save(ddi_merged, "output/codebook.xml")

# ─── XML 文字列として取得（ログ確認・プレビュー用）───
xml_str = s.to_xml_string(ddi_merged)
print(xml_str[:500])
```

#### `validate_structure()` が検出するエラー

| 種別 | 内容 |
|---|---|
| MISSING required | `title` が存在しない |
| MISSING recommended | `abstract`, `principal_investigators`, `universe` のいずれかが存在しない |
| TYPE WARNING | リスト型フィールドに非リスト値が入っている |
| INVALID variables | `variables` の要素が dict でない |

---

### Step 6：人手による確認・修正

LLM の出力には必ず人手確認が必要。特に以下の項目は修正頻度が高い。

| 確認項目 | 理由 |
|---|---|
| `title` | LLM が整形・短縮することがある（原文照合の対象外） |
| `principal_investigators` | 姓名順・ふりがな・欧文スペルの揺れ（原文照合の対象外） |
| `collection_mode` | 自然言語が返ることが多い。統制語彙と照合が必要 |
| `collection_dates` | 出版年と混同しやすい。原文の日付記述を直接確認 |
| 変数のコード値 | `categories` の value-label 対応が逆転・欠落していないか |
| WARNING ログ | `Hallucination guard [study_ids]: ...` 等の出力を確認し、誤って null 化された値は手動で復元する |

---

## 4. 抽出グループとセクション検出のしくみ

### Pass 1：節検出（`_extract_sections()`）

見出し行を1行単位で正規表現照合し、文書を論理的な節に分割する。
英語・日本語の両方に対応している。

| 節名 | 検出パターン例（英語） | 検出パターン例（日本語） |
|---|---|---|
| `preamble` | （見出し前のすべてのテキスト） | — |
| `abstract` | `Abstract`, `Summary` | `要旨`, `概要`, `抄録` |
| `introduction` | `Introduction`, `Background` | `はじめに`, `序論`, `背景` |
| `methods` | `Methods`, `Methodology`, `Research Design` | `方法`, `調査方法`, `研究デザイン` |
| `participants` | `Participants`, `Sample`, `Respondents` | `対象者`, `調査対象`, `サンプル` |
| `data_collection` | `Data Collection`, `Fieldwork` | `データ収集`, `調査実施` |
| `survey_items` | `Questionnaire`, `Appendix A` | `調査票`, `質問票`, `付録` |

> **フォールバック：** 節が検出できなかった場合、全文テキストをトークン予算内に
> トリミングして使用する。

> **番号付き見出しへの対応（実装済み）：** `「1. Methods」`「`3.1 調査方法`」`「第2章 方法論`」`「II. Methods`」
> 等の番号付き見出しは `_NUMBERED_HEADING_PREFIX` 正規表現によりプレフィックスを除去したうえでパターンマッチを行う。
> アラビア数字・ローマ数字・第N章/節・英字接頭辞の4形式に対応している。

### Pass 2：グループ別LLM抽出（`_extract_group()`）

節テキストを3グループに分けてLLMを1グループにつき1回呼び出す。

| グループ名 | 使用節 | 抽出フィールド数 |
|---|---|---|
| `bibliographic` | preamble / abstract / introduction | 9フィールド |
| `methodology` | methods / participants / data_collection | 11フィールド |
| `survey` | survey_items | 1フィールド（`variables` リスト） |

各呼び出しのプロンプト構造（C1 Hallucination対策実装後）：

```
[役割指示]  You are a social-science metadata expert.

[幻覚防止]  EXTRACTION RULES — mandatory, no exceptions:      ← _HALLUCINATION_GUARD (L1)
            1. Extract ONLY information EXPLICITLY present in the text.
            2. Do NOT use general world knowledge or make inferences.
            3. Do NOT invent identifiers, dates, names, or institution names.
            4. If a field is absent, output null — never guess.
            5. When in doubt, prefer null over an uncertain value.

[グループ指示] Extract bibliographic metadata. …               ← EXTRACTION_GROUPS instructions (L2)
            IMPORTANT — 'study_ids': official archive ID only, return null if absent.
            IMPORTANT — 'funding_agencies': funding statement only, do not infer.

[フィールドリスト] Fields to extract: title, abstract, …

[RAG事例]   --- Example outputs ---
            title: ALLBUS 1980
            abstract: …
            --- End examples ---

[対象テキスト] Text to analyse:
            === ABSTRACT ===
            本調査は…
            === INTRODUCTION ===
            …

[出力指示]  YAML output:

↓ LLM の応答後 ↓

[原文照合]  _check_against_source() が study_ids / funding_agencies /  ← L3
            distributors / alternative_title を原文と照合し
            存在しない値を WARNING ログ出力のうえ自動 null 化
```

---

## 5. 残課題と優先度

### カテゴリ別課題マップ

```
DDI生成パイプライン の残課題

├── A. 文書変換層
│    ├── A1. スキャン PDF の OCR 精度（中）
│    ├── ~~A2. 表組み・2段組レイアウト崩れ~~ ✅ 実装済み
│    └── A3. 日本語フォント処理の不安定性（低）
│
├── B. 節検出層
│    ├── ~~B1. 番号付き見出しへの未対応~~ ✅ 実装済み
│    ├── B2. 機関・報告書ごとの見出し表現の差異（中）
│    └── ~~B3. 構造化されていない調査票~~ ✅ 実装済み
│
├── C. LLM 抽出層
│    ├── ~~C1. Hallucination（幻覚）対策~~ ✅ 実装済み
│    ├── ~~C2. 変数の枝分かれ構造（フィルター質問）~~ ✅ 実装済み
│    ├── ~~C3. 変数件数上限（100件）~~ ✅ 実装済み
│    └── ~~C4. 統制語彙への自動マッピング~~ ✅ 実装済み
│
├── D. 複数文書統合
│    ├── ~~D1. 複数ファイル一括処理~~ ✅ 実装済み
│    └── D2. 報告書と調査票の情報矛盾検出（低）
│
├── E. 出力層
│    ├── E1. DDI XML シリアライザ ✅ 実装済み
│    └── E2. XSD スキーマバリデーション（低）
│
└── F. 品質評価
     ├── F1. 自動評価指標（低）
     └── F2. 人手確認 UI（中）
```

---

### A. 文書変換層の課題

#### A1. スキャン PDF の OCR 精度

| 項目 | 内容 |
|---|---|
| **問題** | 紙調査票をスキャンした PDF はテキストデータを含まず、`textract + tesseract` による OCR が必要。日本語の認識精度が不安定 |
| **影響** | 変数名・選択肢コード・質問文の誤認識 → 変数情報が破損 |
| **対策案** | ① Google Cloud Vision / AWS Textract 等の商用 OCR API に切り替える ② 事前に Adobe Acrobat でテキスト化してから処理する |

#### ~~A2. 表組み・2段組レイアウト崩れ~~ ✅ 実装済み

| 項目 | 内容 |
|---|---|
| **問題（解決済み）** | 調査票は表形式（質問番号 \| 質問文 \| 選択肢）が多く、`extract_text()` のみでは列順が崩れ、変数の `categories` の value-label 対応が逆転・欠落していた |
| **実装** | `_extract_pdf_page_text()` メソッドを新規追加。`pdfplumber.find_tables()` でページ内のテーブル領域を検出し、テーブル部分はパイプ区切り構造テキスト（`\| Q1 \| 性別 \| 1.男 2.女 \|`）に変換。テーブル外の領域は `page.crop()` で切り出して通常テキスト抽出。上から下の順に結合して返す |
| **フォールバック** | `find_tables()` が例外を返す場合・テーブルが検出されない場合・構造抽出が空になる場合は `extract_text()` にフォールバック |
| **無効化** | `PaperToDDIAgent(enable_table_extraction=False)` で無効化可能。テーブル検出が過剰に働く文書で利用 |

---

### B. 節検出層の課題

#### ~~B1. 番号付き見出しへの未対応~~ ✅ 実装済み

| 項目 | 内容 |
|---|---|
| **問題（解決済み）** | 「`Ⅲ 調査方法`」「`第3章 方法論`」「`3.1 Methods`」「`II. Methods`」等の番号付き見出しを検出できなかった |
| **実装内容** | `_match_section_heading()` に `_NUMBERED_HEADING_PREFIX` 正規表現を追加。アラビア数字 (`1.` / `3.1` / `2)`), ローマ数字 (`I.` / `II)`), 日本語章節表記 (`第1章` / `第二節`), 英字 (`A.` / `B)`) の各プレフィックスを除去してからパターンマッチを試みる |
| **効果** | 25 テストケースすべてパス。番号付き見出しを持つ報告書でも節検出が正常動作するようになった |

#### ~~B3. 構造化されていない調査票~~ ✅ 実装済み

| 項目 | 内容 |
|---|---|
| **問題（解決済み）** | 見出しなしで質問が羅列されている調査票では節検出が機能しない。全文が "preamble" 扱いになり、`survey` グループが `survey_items` 節を見つけられなかった |
| **実装** | `_promote_survey_items()` メソッドを新規追加。`_extract_sections()` の末尾で呼び出し、`survey_items` 節が未検出かつ preamble に `_B3_MIN_QUESTION_COUNT`（デフォルト 3）件以上の質問境界（`_QUESTION_BOUNDARY_RE`）が検出された場合、preamble を最初の質問境界で分割する。質問前のテキストは preamble に残し、質問以降を `survey_items` に昇格する |
| **対応パターン** | Q1/F1/SQ1（英語）/ 問1/第1問（日本語）/ `\| Q1 \|`（A2 パイプ表形式） |
| **誤検出防止** | 閾値（`_B3_MIN_QUESTION_COUNT = 3`）により、報告書中に "Q1 では〜" のような記述が1〜2行あっても誤って昇格しない |
| **既存見出しとの共存** | `survey_items` 見出し（"調査票" 等）が検出済みの場合は昇格をスキップする。見出し優先 |
| **無効化** | `PaperToDDIAgent(enable_survey_promotion=False)` で無効化可能 |

---

### C. LLM 抽出層の課題

#### ~~C1. Hallucination（幻覚）対策~~ ✅ 実装済み

| 項目 | 内容 |
|---|---|
| **問題（解決済み）** | 文書に明記されていないフィールドを LLM が推測・生成していた（`study_ids` の捏造、`funding_agencies` の推測、`collection_dates` の誤変換が頻発） |
| **L1: プロンプト強化** | `_HALLUCINATION_GUARD` 定数（5条の必須ルール）を全グループのプロンプト冒頭に挿入。「明示されていない情報は抽出しない」「推測しない」「null を優先する」を明示 |
| **L2: フィールド別警告** | `bibliographic` グループの `instructions` に `study_ids`・`funding_agencies` 専用注意書きを追加。`methodology` グループに `collection_dates` 専用注意書きを追加 |
| **L3: 原文照合チェック** | `_check_against_source()` メソッドを追加。抽出完了後に `study_ids`・`funding_agencies`・`distributors`・`alternative_title` の値を原文テキストと照合し、存在しない値を WARNING ログ出力のうえ自動 null 化 |
| **無効化オプション** | `PaperToDDIAgent(enable_source_check=False)` で照合チェックを無効化可能 |

#### ~~C2. 変数の枝分かれ構造（フィルター質問）~~ ✅ 実装済み

| 項目 | 内容 |
|---|---|
| **問題（解決済み）** | 調査票には「Q3で『はい』と答えた方のみQ4へ」等のフィルター条件がある。DDI は `<var>` の `filtrInstr` 要素でこれを表現できるが、現行の `variables` 抽出はフラットな list しか生成しない |
| **実装** | `EXTRACTION_GROUPS["survey"]["instructions"]` に `filter_condition` フィールドを追加（スキップ条件を文字列で記述、無条件の場合は null）。`ddi_xml_serializer.py` の `_build_var()` を拡張し、`filter_condition` が存在する場合は `<qstn>` 配下に `<filtrInstr>` 要素として出力。`question` が null でも `filter_condition` 単独で `<qstn><filtrInstr>` を生成できる |
| **DDI 表現** | `<var><qstn><qstnLit>質問文</qstnLit><filtrInstr>フィルター条件</filtrInstr></qstn></var>` |
| **後方互換** | `filter_condition` キーが存在しない・null の場合は従来通り `<filtrInstr>` を出力しない。既存の `variables` データは変更不要 |

#### ~~C3. 変数件数上限（100件）~~ ✅ 実装済み

| 項目 | 内容 |
|---|---|
| **問題（解決済み）** | 大規模調査（変数300件以上）では調査票全文がトークン上限を超え、プロンプトの「100件上限」指示により後半の変数が抽出されなかった |
| **実装** | `_split_survey_blocks()` メソッドを新規追加。`_QUESTION_BOUNDARY_RE` 正規表現（Q1/F1/SQ1/問1/第1問/パイプ表形式に対応）で質問境界を検出し、`vars_per_block`（デフォルト50）件ごとにテキストを分割。`_extract_survey_variables_chunked()` がブロックごとに LLM を1回呼び出し、`variables` リストを結合して返す |
| **対象パターン** | Q1, F1, SQ1（英語）/ 問1, 第1問（日本語）/ `\| Q1 \|`（A2 パイプ表形式） |
| **設定** | `PaperToDDIAgent(vars_per_block=50)` でブロックサイズを変更可能。大きなコンテキストウィンドウのモデルでは増やすと LLM 呼び出し回数を削減できる |
| **後方互換** | 変数数が `vars_per_block` 以下の場合は従来通り1回の LLM 呼び出しに留まる |

#### ~~C4. 統制語彙への自動マッピング~~ ✅ 実装済み

| 項目 | 内容 |
|---|---|
| **問題（解決済み）** | LLM が返す自然言語（「郵送調査」「横断調査」等）を DDI Alliance CV コードに変換できていなかった |
| **実装** | `wrappers/social/ddi_cv_mapper.py` を新規作成。正規表現パターン辞書（英語・日本語混在）で5フィールドをマッピング |
| **対応フィールドと CV** | `collection_mode` → ModeOfCollection 3.0 / `time_method` → TimeMethod 1.2 / `analysis_unit` → AnalysisUnit 1.1 / `sampling_procedure` → SamplingProcedure 1.0 / `topic_classification` → **CESSDA TopicClassification 4.2.2**（82コード） |
| **list フィールド対応** | `topic_classification` はリスト型のため各要素を個別にマッピングし、`topic_classification_cv` にコードのリストを格納する |
| **出力形式** | 元の自然言語値は保持し、`{field}_cv` / `{field}_cv_list_id` / `{field}_cv_list_ver` の3並列キーを追加 |
| **XML 連携** | `DDIXMLSerializer` を更新。`_cv` キーがある場合は `codeListID`・`codeListVersionID` 属性を XML 要素に付加。`<topcClas>` も CESSDA CV コード + 属性で出力 |
| **無効化** | `PaperToDDIAgent(enable_cv_mapping=False)` で無効化可能 |

---

### D. 複数文書統合の課題

#### ~~D1. 複数ファイル一括処理~~ ✅ 実装済み

| 項目 | 内容 |
|---|---|
| **問題（解決済み）** | 現行の `PaperToDDIAgent` は1ファイル入力のみ。調査報告書と調査票を別々に処理してから手動でマージする必要があった |
| **実装** | `extract_ddi_from_files(files)` メソッドを追加。`files` に `{"report": "report.pdf", "questionnaire": "questionnaire.pdf"}` のようなロール→パスのdictを渡すと自動マージする |
| **ロール定義** | `"report"` → bibliographic + methodology グループ／`"questionnaire"` → survey グループ（変数）／`"all"` → 全グループ |
| **プレフィックス一致** | `"report_2023"` → report ロール、`"questionnaire_wave2"` → questionnaire ロールのように、ロール名の前方一致で解決する |
| **複数ファイル** | リスト形式 `[("report","r1.pdf"),("report","r2.pdf"),("questionnaire","q.pdf")]` で同一ロール複数ファイルにも対応 |
| **source-check** | 各ファイルの抽出直後にそのファイルの原文に対してHallucination照合を実施。CV マッピングは全ファイルマージ後に一括適用 |
| **後方互換** | `extract_ddi_from_file()` / `extract_ddi_from_text()` の既存API は変更なし |

---

### E. 出力層の課題

#### E1. DDI XML シリアライザ ✅ 実装済み

`DDIXMLSerializer` (`ddi_xml_serializer.py`) が実装済み。
27フィールド + 変数定義に対応した DDI-Codebook 2.5 準拠 XML を標準ライブラリのみで生成する。

#### E2. XSD スキーマバリデーション

| 項目 | 内容 |
|---|---|
| **現状** | `validate_structure()` で主要フィールドの構造検証のみ実施。DDI 2.5 XSD に対する正式バリデーションは未実装 |
| **対策案** | `lxml.etree.XMLSchema` と DDI 公式 XSD を用いたバリデーションを追加する |

---

### F. 品質評価の課題

#### F2. 人手確認 UI

| 項目 | 内容 |
|---|---|
| **問題** | 変数情報（100件超）の人手確認は高コスト。特にコード値と選択肢ラベルの対応検証は専門知識が必要 |
| **対策案** | Streamlit UI にフィールドごとの確認チェックボックスを追加する（未実装） |

---

## 6. 課題の優先度マトリクス

| # | 課題 | 影響 | 工数 | 優先度 |
|---|---|---|---|---|
| ~~B1~~ | ~~番号付き見出しへの未対応~~ | ✅ 実装済み | — | **完了** |
| ~~C1~~ | ~~Hallucination 対策~~ | ✅ 実装済み | — | **完了** |
| ~~D1~~ | ~~複数ファイル一括処理~~ | ✅ 実装済み | — | **完了** |
| ~~A2~~ | ~~表組みレイアウト崩れ~~ | ✅ 実装済み | — | **完了** |
| ~~C3~~ | ~~変数件数上限~~ | ✅ 実装済み | — | **完了** |
| ~~C4~~ | ~~統制語彙マッピング~~ | ✅ 実装済み | — | **完了** |
| ~~B3~~ | ~~非構造化調査票~~ | ✅ 実装済み | — | **完了** |
| ~~C2~~ | ~~フィルター質問~~ | ✅ 実装済み | — | **完了** |
| F2 | 人手確認 UI | 中（運用コスト削減） | 大（Streamlit実装） | **低** |
| E2 | XSD バリデーション | 低（正式検証なし） | 小（lxml追加） | **低** |
| A1 | OCR 精度 | 中（スキャン PDF のみ） | 大（外部 API 導入） | **低** |

---

## 7. 今後の実装ロードマップ（案）

```
フェーズ1（小工数・高効果）
├── B1: 番号付き見出しの正規表現パターン拡張 ✅ 実装済み
│    → _NUMBERED_HEADING_PREFIX で番号プレフィックスを除去してからパターンマッチ
├── C1: Hallucination 抑制 ✅ 実装済み
│    → _HALLUCINATION_GUARD（L1）+ フィールド別警告（L2）+ 原文照合チェック（L3）
└── C3: 変数の分割処理（100件超対応）✅ 実装済み
     → _split_survey_blocks() + _extract_survey_variables_chunked()
        _QUESTION_BOUNDARY_RE で質問境界検出 → vars_per_block=50 件ごとにブロック分割
        ブロック単位でLLM呼び出し → variables リストを結合して返す

フェーズ2（中工数・中効果）
├── D1: 複数ファイル統合メソッドの追加 ✅ 実装済み
│    → extract_ddi_from_files({role: path}) / [(role, path), ...]
├── A2: pdfplumber.extract_table() による表形式調査票の対応 ✅ 実装済み
│    → _extract_pdf_page_text(): find_tables() + crop() + _table_to_text()
└── C4: 統制語彙マッピング ✅ 実装済み
     → ddi_cv_mapper.py（ModeOfCollection / TimeMethod / AnalysisUnit /
        SamplingProcedure / CESSDA TopicClassification 4.2.2 全82コード対応）

フェーズ2.5（追加実装）
└── B3: 非構造化調査票の survey_items 自動昇格 ✅ 実装済み
     → _promote_survey_items(): _extract_sections() 末尾で呼び出し
        _QUESTION_BOUNDARY_RE で preamble を走査 → 閾値（3件）以上で昇格
        質問前テキストを preamble に保持、Q1 以降を survey_items に昇格

フェーズ2.8（追加実装）
└── C2: フィルター質問の枝分かれ構造対応 ✅ 実装済み
     → EXTRACTION_GROUPS["survey"]["instructions"] に filter_condition フィールドを追加
        ddi_xml_serializer._build_var() を拡張し <qstn><filtrInstr> を出力
        question=None でも filter_condition 単独で <qstn><filtrInstr> を生成

フェーズ3（大工数・将来対応）
├── F2: Streamlit UI による確認支援画面
└── E2: DDI XSD スキーマバリデーション
```

---

## 8. 参考：実装済みコンポーネントの API 早見表

### DDI 事例収集

```python
from curategpt.wrappers.social.ddi_codebook_wrapper import DDICodebookWrapper, SSJDA_OAI_URL

wrapper = DDICodebookWrapper(oai_base_url=SSJDA_OAI_URL, max_records=100)
for obj in wrapper.objects():   # OAI-PMH から全件収集
    print(obj["title"])
```

### 全文読み込み

```python
from curategpt.wrappers.social.full_document_wrapper import FullDocumentWrapper

wrapper = FullDocumentWrapper(root_directory="./docs", glob="*.pdf")
for obj in wrapper.objects():   # チャンクなしで1ファイル=1レコード
    print(obj["name"], len(obj["text"]))
```

### メタデータ抽出

```python
from curategpt.agents.paper_to_ddi_agent import PaperToDDIAgent

agent = PaperToDDIAgent(
    knowledge_source=db,
    extractor=extractor,
    enable_source_check=True,   # Hallucination 照合チェック（デフォルト有効）
    enable_cv_mapping=True,     # 統制語彙マッピング（デフォルト有効）
)

# 単一ファイル
ddi = agent.extract_ddi_from_file("survey_report.pdf")
ddi = agent.extract_ddi_from_text(text_str, filename="report.txt")

# 複数ファイル（D1 実装済み）
ddi = agent.extract_ddi_from_files({
    "report":        "survey_report.pdf",   # bibliographic + methodology
    "questionnaire": "questionnaire.pdf",   # survey (variables)
})

# 同一ロール複数ファイル（リスト形式）
ddi = agent.extract_ddi_from_files([
    ("report",        "report_wave1.pdf"),
    ("report",        "report_wave2.pdf"),
    ("questionnaire", "questionnaire.pdf"),
])
```

### XML 出力

```python
from curategpt.wrappers.social.ddi_xml_serializer import DDIXMLSerializer, to_ddi_xml

s = DDIXMLSerializer()
s.save(ddi, "output.xml")                           # ファイル保存
xml_str = s.to_xml_string(ddi)                      # 文字列取得
ok, errors = s.validate_structure(ddi)              # 構造検証
to_ddi_xml(ddi, file_path="output.xml")             # 1行ショートカット
```

---

## 9. 各実装の詳細ドキュメント

各課題の実装詳細は個別のサマリーファイルを参照。

| ファイル | 対応課題 | 内容 |
|---|---|---|
| `DDI_WRAPPER_SUMMARY.md` | — | DDICodebookWrapper・FullDocumentWrapper の実装詳細 |
| `DDI_TABLE_EXTRACTION_SUMMARY.md` | A2 | pdfplumber による表形式調査票の構造抽出 |
| `DDI_HALLUCINATION_GUARD_SUMMARY.md` | C1 | L1/L2/L3 三層 Hallucination 対策 |
| `DDI_FILTER_QUESTION_SUMMARY.md` | C2 | filter_condition フィールドと `<filtrInstr>` XML 出力 |
| `DDI_CHUNKED_SURVEY_SUMMARY.md` | C3 | 大規模調査票のブロック分割・変数全件抽出 |
| `DDI_UNSTRUCTURED_SURVEY_SUMMARY.md` | B3 | 見出しなし調査票の survey_items 自動昇格 |
| `DDI_MULTI_FILE_SUMMARY.md` | D1 | 複数ファイル一括処理・ロール別マージ |
