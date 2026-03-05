# 機能仕様書
## DDI-Codebook メタデータ自動生成システム（CurateGPT 拡張）

**文書種別**: 機能仕様書
**対象バージョン**: CurateGPT v0.2.4 + DDI パイプライン拡張
**作成日**: 2026-03-05
**対象規格**: DDI-Codebook 2.5

---

## 目次

1. [概要](#1-概要)
2. [システム構成](#2-システム構成)
3. [新規追加ファイル一覧](#3-新規追加ファイル一覧)
4. [コンポーネント仕様](#4-コンポーネント仕様)
   - 4.1 DDICodebookWrapper
   - 4.2 FullDocumentWrapper
   - 4.3 PaperToDDIAgent
   - 4.4 DDICVMapper
   - 4.5 DDIXMLSerializer
5. [データモデル仕様](#5-データモデル仕様)
6. [設定パラメータ一覧](#6-設定パラメータ一覧)
7. [エラー処理とフォールバック](#7-エラー処理とフォールバック)
8. [テスト仕様](#8-テスト仕様)

---

## 1. 概要

### 1.1 目的

本システムは、社会調査の **調査票（PDF）** および **調査報告書（PDF）** を入力として、
**DDI-Codebook 2.5** 準拠のメタデータを自動生成するパイプラインである。

CurateGPT の RAG（Retrieval-Augmented Generation）基盤に乗せる形で、
以下の処理を実現している。

1. 既存 DDI リポジトリからの事例収集（RAG 知識ベース構築）
2. PDF 全文読み込み（チャンク分割なし）
3. 節検出による文書構造の把握
4. グループ別 LLM 呼び出しによるメタデータ抽出
5. 統制語彙への自動マッピング
6. DDI-Codebook 2.5 準拠 XML の出力

### 1.2 ベースシステムとの関係

CurateGPT（ベースシステム）は以下の機能を提供する。

| ベース機能 | クラス | 本拡張での利用方法 |
|---|---|---|
| ベクトル DB 操作 | `ChromaDBAdapter` | DDI 事例のインデックス・検索に使用 |
| LLM 呼び出し | `BasicExtractor` | DDI フィールド抽出の LLM インタフェース |
| エージェント基底 | `BaseAgent` | `PaperToDDIAgent` の親クラス |
| Wrapper 基底 | `BaseWrapper` | `DDICodebookWrapper`, `FullDocumentWrapper` の親クラス |

### 1.3 処理フロー概要

```
【入力】                    【処理】                              【出力】

調査報告書 (PDF)  ──┐
                    │ FullDocumentWrapper（全文読み込み）
調査票 (PDF)      ──┤         │
                    │         ▼
                    │  PaperToDDIAgent
                    │  ┌─────────────────────────────────────┐
                    │  │ Pass 1: 節検出                       │
                    │  │  ├─ 見出し正規表現マッチ（EN/JA）    │
                    │  │  └─ 非構造化調査票の自動昇格（B3）   │
                    │  │ Pass 2: グループ別 LLM 抽出          │◄── ChromaDB
                    │  │  ├─ bibliographic グループ           │    （RAG事例）
                    │  │  ├─ methodology グループ             │
                    │  │  └─ survey グループ（ブロック分割）  │
                    │  │ 後処理                               │
                    │  │  ├─ Hallucination 原文照合（C1 L3）  │
                    │  │  └─ 統制語彙マッピング（C4）         │
                    │  └─────────────────────────────────────┘
                    │         │
                    │         ▼
                    └──► DDI metadata dict
                                │
                                ▼
                         DDIXMLSerializer
                                │
                                ▼
                         DDI-Codebook 2.5 XML ──► output.xml
```

---

## 2. システム構成

### 2.1 ファイル構成（新規追加分）

```
src/curategpt/
├── agents/
│   └── paper_to_ddi_agent.py          # DDI 抽出エージェント（中核）
└── wrappers/
    └── social/
        ├── __init__.py                 # パッケージマーカー
        ├── ddi_codebook_wrapper.py     # OAI-PMH 事例収集ラッパー
        ├── full_document_wrapper.py    # 全文読み込みラッパー
        ├── ddi_cv_mapper.py            # 統制語彙マッパー
        └── ddi_xml_serializer.py       # DDI XML 出力シリアライザ

tests/
├── agents/
│   ├── test_paper_to_ddi_b3.py        # B3 非構造化調査票テスト（22ケース）
│   └── test_paper_to_ddi_c3.py        # C3 分割抽出テスト（26ケース）
└── wrappers/
    ├── test_ddi_xml_serializer.py      # XML シリアライザテスト
    └── test_ddi_xml_serializer_c2.py   # C2 フィルター質問テスト（13ケース）
```

### 2.2 依存関係

| ライブラリ | バージョン | 用途 | 必須度 |
|---|---|---|---|
| `pdfplumber` | 任意 | PDF テキスト・表抽出 | 強く推奨 |
| `textract` | 任意 | DOCX/XLS 等の抽出 | PDF のみなら不要 |
| `llm` + `llm-openai` | ^0.15 | LLM API 呼び出し | 必須 |
| `pyyaml` | 任意 | LLM 応答の YAML パース | 必須 |
| `xmltodict` | 任意 | OAI-PMH XML 解析 | DDI 事例収集時に必須 |
| `xml.etree.ElementTree` | 標準ライブラリ | DDI XML 生成 | 不要（標準） |

---

## 3. 新規追加ファイル一覧

| ファイル | 役割 | 主要クラス/関数 |
|---|---|---|
| `ddi_codebook_wrapper.py` | OAI-PMH リポジトリから DDI 事例を収集し ChromaDB へ格納する | `DDICodebookWrapper` |
| `full_document_wrapper.py` | PDF/DOCX を3,000文字チャンクに分割せず1件として読み込む | `FullDocumentWrapper` |
| `paper_to_ddi_agent.py` | 調査票・報告書の全文から DDI メタデータを抽出する | `PaperToDDIAgent` |
| `ddi_cv_mapper.py` | LLM 出力の自然言語値を DDI Alliance 統制語彙コードにマッピングする | `apply_cv_mappings()` |
| `ddi_xml_serializer.py` | DDI dict を DDI-Codebook 2.5 準拠 XML に変換する | `DDIXMLSerializer`, `to_ddi_xml()` |

---

## 4. コンポーネント仕様

---

### 4.1 DDICodebookWrapper

**ファイル**: `src/curategpt/wrappers/social/ddi_codebook_wrapper.py`

#### 4.1.1 目的

OAI-PMH プロトコルを通じて国際 DDI リポジトリからメタデータ事例を取得し、
CurateGPT の `ChromaDBAdapter` にインデックスする。
`PaperToDDIAgent` の RAG 知識ベースを構築するために使用する。

#### 4.1.2 対応リポジトリ

| 定数名 | URL | 収録内容 |
|---|---|---|
| `GESIS_OAI_URL` | `https://api.gesis.org/DBK/OAI-PMH` | 欧州社会科学（英語・独語中心） |
| `ICPSR_OAI_URL` | `https://www.icpsr.umich.edu/icpsrweb/ICPSR/oai/studies` | 米国社会科学 |
| `UKDS_OAI_URL` | `https://oai.ukdataservice.ac.uk/oai/provider` | 英国データサービス |
| `SSJDA_OAI_URL` | `https://ssjda.iss.u-tokyo.ac.jp/oai` | 日本（東大社会科学研究所） |

#### 4.1.3 主要メソッド

| メソッド | シグネチャ | 説明 |
|---|---|---|
| `objects()` | `() → Iterator[dict]` | OAI-PMH 全件収集。`max_records` 件まで DDI レコードを yield する |

#### 4.1.4 出力フィールド

`objects()` が返す dict は以下のキーを含む（DDI XML から変換）。

```
id, title, alternative_title, abstract, principal_investigators,
keywords, topic_classification, nations, universe, sampling_procedure,
collection_mode, time_method, variables
```

#### 4.1.5 設定パラメータ

| パラメータ | 型 | デフォルト | 説明 |
|---|---|---|---|
| `oai_base_url` | str | GESIS_OAI_URL | OAI-PMH エンドポイント URL |
| `max_records` | int | 200 | 収集上限件数 |
| `metadata_prefix` | str | `"oai_ddi25"` | OAI-PMH メタデータプレフィックス |

#### 4.1.6 使用例

```python
from curategpt.store import ChromaDBAdapter
from curategpt.wrappers.social.ddi_codebook_wrapper import DDICodebookWrapper, SSJDA_OAI_URL

db = ChromaDBAdapter("~/.curategpt/db")
wrapper = DDICodebookWrapper(oai_base_url=SSJDA_OAI_URL, max_records=200)
db.upsert(list(wrapper.objects()), collection="ddi_examples", model="openai:")
```

---

### 4.2 FullDocumentWrapper

**ファイル**: `src/curategpt/wrappers/social/full_document_wrapper.py`

#### 4.2.1 目的

CurateGPT の `FilesystemWrapper` はドキュメントを3,000文字チャンクに分割して格納するが、
本ラッパーはチャンク分割を行わず、**1ファイル＝1レコード**として読み込む。

これにより `PaperToDDIAgent` が文書全文を節単位で処理できるようになる。

#### 4.2.2 PDF 抽出の優先順位

1. **pdfplumber**（推奨）: レイアウト保持精度が高い
2. **textract**（フォールバック）: pdfplumber が利用不可の場合
3. **エラー時**: 空文字列を返してログに記録

#### 4.2.3 主要メソッド

| メソッド | シグネチャ | 説明 |
|---|---|---|
| `objects()` | `() → Iterator[dict]` | `root_directory` 以下のファイルを1件ずつ yield |

#### 4.2.4 出力フィールド

```python
{
    "name":  "questionnaire.pdf",   # ファイル名
    "text":  "（全文テキスト）",     # チャンク分割なし
    "path":  "/absolute/path/...",  # 絶対パス
}
```

---

### 4.3 PaperToDDIAgent

**ファイル**: `src/curategpt/agents/paper_to_ddi_agent.py`

本システムの中核コンポーネント。調査票・調査報告書の全文を入力として
DDI メタデータ dict を返す。

#### 4.3.1 概要

2パス構成の抽出エージェント。

- **Pass 1（節検出）**: 正規表現による見出しパターンマッチと非構造化調査票の自動昇格
- **Pass 2（LLM 抽出）**: 3グループに分けて LLM を呼び出し、結果をマージ

#### 4.3.2 Pass 1：節検出（`_extract_sections()`）

##### 4.3.2.1 基本動作

文書テキストを1行単位で走査し、見出し行を正規表現で照合して節に分割する。
見出し前のすべてのテキストは `preamble` 節として格納する。

##### 4.3.2.2 検出節一覧

| 節名 | 英語パターン例 | 日本語パターン例 |
|---|---|---|
| `preamble` | （見出し前のすべてのテキスト） | — |
| `abstract` | `Abstract`, `Summary` | `要旨`, `概要`, `抄録` |
| `introduction` | `Introduction`, `Background` | `はじめに`, `序論`, `背景` |
| `methods` | `Methods`, `Methodology`, `Research Design` | `方法`, `調査方法`, `研究デザイン` |
| `participants` | `Participants`, `Sample`, `Respondents` | `対象者`, `調査対象`, `サンプル` |
| `data_collection` | `Data Collection`, `Fieldwork` | `データ収集`, `調査実施` |
| `survey_items` | `Questionnaire`, `Appendix A` | `調査票`, `質問票`, `付録` |
| `results` | `Results`, `Findings` | `結果`, `分析結果` |
| `discussion` | `Discussion`, `Conclusion` | `考察`, `結論` |
| `references` | `References`, `Bibliography` | `参考文献`, `文献` |

`results`・`discussion`・`references` は検出するが LLM 抽出には使用しない（`_SKIP_SECTIONS`）。

##### 4.3.2.3 番号付き見出しへの対応（B1）

見出し行にある番号プレフィックスを `_NUMBERED_HEADING_PREFIX` 正規表現で除去してからパターンマッチを試みる。

| 対応形式 | 例 |
|---|---|
| アラビア数字 | `1. Methods`, `3.1 調査方法`, `2)` |
| ローマ数字 | `II. Methods`, `Ⅲ 方法` |
| 日本語章節 | `第2章 方法論`, `第一節 方法` |
| 英字 | `A. Methods`, `B)` |

##### 4.3.2.4 非構造化調査票の自動昇格（B3）

見出しなしで質問が羅列されている調査票（`survey_items` 節が未検出）を処理するため、
`_extract_sections()` の末尾で `_promote_survey_items()` を呼び出す。

**動作条件**:
- `enable_survey_promotion = True`（デフォルト）
- `survey_items` 節が未検出
- `preamble` テキスト中に `_QUESTION_BOUNDARY_RE` が `_B3_MIN_QUESTION_COUNT`（3）件以上マッチ

**処理結果**:
- 最初の質問境界より前のテキスト → `preamble` に保持
- 最初の質問境界以降のテキスト → `survey_items` に昇格

**検出パターン（`_QUESTION_BOUNDARY_RE`）**:

| パターン | 例 |
|---|---|
| 英語 Q スタイル | `Q1.`, `Q 12`, `q3)` |
| フォーム変数コード | `F1.`, `F 2` |
| サブ質問コード | `SQ1.`, `SQ 3` |
| 日本語 問N | `問1`, `問 2.` |
| 日本語 第N問 | `第1問`, `第 2 問` |
| A2 パイプ表形式 | `\| Q1 \|`, `\| 問3 \|` |

#### 4.3.3 PDF テーブル抽出（A2）

PDF 内の表形式レイアウトを保持するため、`pdfplumber` の `find_tables()` を使用した
構造的テキスト抽出を実装している（`_extract_pdf_page_text()`）。

**処理フロー（`enable_table_extraction=True` の場合）**:

```
PDFページ
├─ find_tables() でテーブル領域を検出
│    ├─ テーブル領域 → _table_to_text() でパイプ区切りテキストに変換
│    │    例: "| Q1 | 性別 | 1.男性 2.女性 |"
│    └─ テーブル外領域 → page.crop() で切り出して extract_text()
└─ 上から下の順に結合して返す
```

**フォールバック条件**:
- `pdfplumber` が利用不可
- `find_tables()` が例外を返す
- テーブルが検出されない
- 構造抽出結果が空

上記いずれかの場合、`extract_text()` の結果を返す。

#### 4.3.4 Pass 2：グループ別 LLM 抽出（`_run_extraction_groups()`）

節テキストを3グループに分けて LLM を呼び出す。

| グループ名 | 使用節 | 抽出フィールド |
|---|---|---|
| `bibliographic` | `preamble`, `abstract`, `introduction` | `title`, `alternative_title`, `abstract`, `principal_investigators`, `funding_agencies`, `distributors`, `study_ids`, `keywords`, `topic_classification` |
| `methodology` | `methods`, `participants`, `data_collection` | `time_periods`, `collection_dates`, `nations`, `geographic_coverage`, `universe`, `analysis_unit`, `sampling_procedure`, `collection_mode`, `time_method`, `collection_situation`, `weighting` |
| `survey` | `survey_items` | `variables`（変数リスト） |

#### 4.3.5 プロンプト構成

各 LLM 呼び出しのプロンプトは以下の順で構成される。

```
[役割指示]      You are a social-science metadata expert.

[幻覚防止 L1]   EXTRACTION RULES — mandatory, no exceptions:
                1. Extract ONLY information EXPLICITLY present in the text.
                2. Do NOT use general world knowledge or make inferences.
                3. Do NOT invent identifiers, dates, names, or institution names.
                4. If a field is absent, output null — never guess.
                5. When in doubt, prefer null over an uncertain value.

[グループ指示]  （グループ固有の注意事項 — L2）
                例: IMPORTANT — 'study_ids': official archive ID only.

[フィールドリスト] Fields to extract: title, abstract, ...

[RAG 事例]      --- Example outputs ---
                title: ALLBUS 1980
                ...
                --- End examples ---

[対象テキスト]  Text to analyse:
                === ABSTRACT ===
                本調査は…

[出力指示]      YAML output:
```

#### 4.3.6 Hallucination 対策（C1）

3層構造の Hallucination 抑制機構。

| 層 | 名称 | 実装 |
|---|---|---|
| L1 | プロンプト全体ルール | `_HALLUCINATION_GUARD` 定数を全グループのプロンプト冒頭に挿入 |
| L2 | フィールド別警告 | `bibliographic` に `study_ids`・`funding_agencies` 専用注意書き。`methodology` に `collection_dates` 専用注意書き |
| L3 | 原文照合チェック | `_check_against_source()` が抽出後に `_SOURCE_CHECK_FIELDS` の値を原文と照合し、存在しない値を WARNING ログのうえ自動 null 化 |

**原文照合チェック対象フィールド（`_SOURCE_CHECK_FIELDS`）**:

```
study_ids, funding_agencies, distributors, alternative_title
```

dict 型・list of dict 型（`variables`, `time_periods`）は照合対象外（複数テキスト断片を LLM が組み合わせるため）。

#### 4.3.7 変数分割抽出（C3）

大規模調査票（変数300件以上）でも全変数を抽出するための分割処理。

**`_split_survey_blocks(text)` の動作**:

```
survey_items テキスト
├─ _QUESTION_BOUNDARY_RE.finditer() で境界位置リストを取得
├─ 境界数 ≤ vars_per_block（デフォルト 50）
│    → [text] を返す（分割なし、従来動作）
└─ 境界数 > vars_per_block
     split_points = [0, positions[50], positions[100], ..., len(text)]
     → text[split_points[i]:split_points[i+1]] を各ブロックとして返す
```

**`_extract_survey_variables_chunked(section_text, instructions)` の動作**:

- ブロック数が 1 → `_extract_group()` を 1 回呼び出し
- ブロック数が 2 以上 → ブロックごとに `_extract_group()` を呼び出し、`variables` リストを結合

#### 4.3.8 フィルター質問対応（C2）

調査票のスキップ条件・枝分かれ構造を `filter_condition` フィールドで保持する。

**抽出スキーマ（`variables` の各要素）**:

| フィールド | 型 | 説明 |
|---|---|---|
| `name` | string | 変数コードまたは短ラベル（例: `Q1`, `SEX`） |
| `label` | string | 変数ラベル（完全な質問ラベル） |
| `question` | string \| null | 原文の質問文 |
| `filter_condition` | string \| null | スキップ指示（例: `"Q3で『はい』と答えた方のみ"`）。無条件の場合は null |
| `categories` | list[{value, label}] | 選択肢コードとラベルのリスト |
| `format` | string | `numeric` / `string` / `date` |

#### 4.3.9 複数ファイル一括処理（D1）

`extract_ddi_from_files(files)` により複数ファイルをロール別に処理してマージする。

**ロール定義**:

| ロール名（プレフィックス一致） | 実行グループ |
|---|---|
| `report` | `bibliographic` + `methodology` |
| `questionnaire` | `survey` |
| `all` | 全グループ |

**入力形式**:

```python
# dict 形式
agent.extract_ddi_from_files({
    "report":        "survey_report.pdf",
    "questionnaire": "questionnaire.pdf",
})

# list of tuples 形式（同一ロール複数ファイル）
agent.extract_ddi_from_files([
    ("report",        "report_wave1.pdf"),
    ("report",        "report_wave2.pdf"),
    ("questionnaire", "questionnaire.pdf"),
])
```

**マージルール**:
- 文字列フィールド: 後勝ち（非 null 値で上書き）
- リストフィールド（`variables`, `keywords` 等）: 結合（extend）

**処理順序**:
1. ファイルごとに指定グループのみ LLM 抽出
2. 各ファイルの抽出直後に原文照合チェック（L3）を実施
3. 全ファイルのマージ完了後に統制語彙マッピングを一括適用

#### 4.3.10 トークン予算管理

LLM に渡すテキストをトークン予算内に収めるための文字数制限。

```python
_CHARS_PER_TOKEN   = 4       # 1 token ≈ 4 chars（英日混在テキスト）
_RESERVED_TOKENS   = 1500    # プロンプトオーバーヘッド・RAG 事例・レスポンス用予約
_DEFAULT_CONTEXT_TOKENS = 8000  # デフォルトコンテキストウィンドウ

文字予算 = (context_tokens - 1500) × 4
```

テキストが予算を超えた場合は後半を切り捨てる（冒頭優先）。

---

### 4.4 DDICVMapper

**ファイル**: `src/curategpt/wrappers/social/ddi_cv_mapper.py`

#### 4.4.1 目的

LLM が返す自然言語値（「郵送調査」「横断研究」等）を
DDI Alliance 公式統制語彙コードに変換する。

#### 4.4.2 対応フィールドと統制語彙

| フィールド | 統制語彙 | バージョン | コード例 |
|---|---|---|---|
| `collection_mode` | ModeOfCollection | 3.0 | `SelfAdministeredQuestionnaire.Paper` |
| `time_method` | TimeMethod | 1.2 | `CrossSection` |
| `analysis_unit` | AnalysisUnit | 1.1 | `Individual` |
| `sampling_procedure` | SamplingProcedure | 1.0 | `ProbabilitySimpleRandom` |
| `topic_classification` | TopicClassification（CESSDA） | 4.2.2 | `Health.PublicHealth`（82コード対応） |

#### 4.4.3 変換アルゴリズム

1. フィールド値を小文字に正規化
2. 対応する `_*_PATTERNS` リストに含まれる `(regex, cv_code)` ペアを順番に照合
3. 最初にマッチしたパターンの `cv_code` を採用（先着優先）
4. どのパターンもマッチしない場合は `None`（元の値を保持）

#### 4.4.4 出力フォーマット

元の値を保持した上で3つの並列キーを追加する。

```python
# スカラーフィールドの場合
ddi["collection_mode"]            # "郵送調査"（元の値・保持）
ddi["collection_mode_cv"]         # "SelfAdministeredQuestionnaire.Paper"
ddi["collection_mode_cv_list_id"] # "ModeOfCollection"
ddi["collection_mode_cv_list_ver"]# "3.0"

# リストフィールドの場合（topic_classification）
ddi["topic_classification"]           # ["健康", "政治参加"]（元の値・保持）
ddi["topic_classification_cv"]        # ["Health.PublicHealth", "Politics.ElectionsVoting"]
ddi["topic_classification_cv_list_id"]# "TopicClassification"
ddi["topic_classification_cv_list_ver"]# "4.2.2"
```

#### 4.4.5 主要関数

| 関数 | シグネチャ | 説明 |
|---|---|---|
| `apply_cv_mappings(ddi)` | `(dict) → dict` | DDI dict 全体にマッピングを適用して返す |

---

### 4.5 DDIXMLSerializer

**ファイル**: `src/curategpt/wrappers/social/ddi_xml_serializer.py`

#### 4.5.1 目的

Python dict（`PaperToDDIAgent` の出力）を DDI-Codebook 2.5 準拠の XML に変換する。
標準ライブラリ `xml.etree.ElementTree` のみを使用し、外部依存なし。

#### 4.5.2 XML 名前空間

| 名前空間 | URI |
|---|---|
| デフォルト | `ddi:codebook:2_5` |
| XSI | `http://www.w3.org/2001/XMLSchema-instance` |

#### 4.5.3 フィールドマッピング

| dict キー | DDI XML パス |
|---|---|
| `id` | `codeBook[@ID]` |
| `title` | `stdyDscr/citation/titlStmt/titl` |
| `alternative_title` | `stdyDscr/citation/titlStmt/altTitl` |
| `study_ids` | `stdyDscr/citation/titlStmt/IDNo`（繰り返し） |
| `principal_investigators` | `stdyDscr/citation/rspStmt/AuthEnty`（繰り返し） |
| `funding_agencies` | `stdyDscr/citation/prodStmt/fundAg`（繰り返し） |
| `distributors` | `stdyDscr/citation/distStmt/distrbtr`（繰り返し） |
| `abstract` | `stdyDscr/stdyInfo/abstract` |
| `keywords` | `stdyDscr/stdyInfo/subject/keyword`（繰り返し） |
| `topic_classification` | `stdyDscr/stdyInfo/subject/topcClas`（繰り返し） |
| `time_periods` | `stdyDscr/stdyInfo/sumDscr/timePrd`（繰り返し） |
| `collection_dates` | `stdyDscr/stdyInfo/sumDscr/collDate`（繰り返し） |
| `nations` | `stdyDscr/stdyInfo/sumDscr/nation`（繰り返し） |
| `geographic_coverage` | `stdyDscr/stdyInfo/sumDscr/geogCover`（繰り返し） |
| `universe` | `stdyDscr/stdyInfo/sumDscr/universe` |
| `analysis_unit` | `stdyDscr/stdyInfo/sumDscr/anlyUnit` |
| `sampling_procedure` | `stdyDscr/method/dataColl/sampProc` |
| `collection_mode` | `stdyDscr/method/dataColl/collMode` |
| `time_method` | `stdyDscr/method/dataColl/timeMeth` |
| `collection_situation` | `stdyDscr/method/dataColl/collSitu` |
| `weighting` | `stdyDscr/method/dataColl/weight` |
| `cleaning_operations` | `stdyDscr/method/dataColl/cleanOps` |
| `access_place` | `stdyDscr/dataAccs/setAvail/accsPlac` |
| `access_conditions` | `stdyDscr/dataAccs/useStmt/restrctn` |
| `special_permissions` | `stdyDscr/dataAccs/useStmt/specPerm` |
| `file_name` | `fileDscr/fileTxt/fileName` |
| `file_type` | `fileDscr/fileTxt/fileType` |
| `file_content_description` | `fileDscr/fileTxt/fileCont` |
| `variables` | `dataDscr/var`（リスト） |

#### 4.5.4 変数要素（`<var>`）の構造

```xml
<var ID="V1" name="Q1">
  <labl>質問ラベル</labl>
  <qstn>
    <qstnLit>原文の質問文（question）</qstnLit>
    <filtrInstr>フィルター条件（filter_condition）</filtrInstr>  <!-- C2 -->
  </qstn>
  <catgry>
    <catValu>1</catValu>
    <labl>男性</labl>
  </catgry>
  <catgry>
    <catValu>2</catValu>
    <labl>女性</labl>
  </catgry>
  <varFormat type="numeric"/>
</var>
```

**`<qstn>` 生成ルール（C2 実装後）**:

| `question` | `filter_condition` | 出力 |
|---|---|---|
| あり | あり | `<qstn><qstnLit>…</qstnLit><filtrInstr>…</filtrInstr></qstn>` |
| あり | null/なし | `<qstn><qstnLit>…</qstnLit></qstn>` |
| null | あり | `<qstn><filtrInstr>…</filtrInstr></qstn>` |
| null | null/なし | `<qstn>` 要素自体なし |

#### 4.5.5 統制語彙属性の付加

`apply_cv_mappings()` 適用後の dict には `{field}_cv`・`{field}_cv_list_id`・`{field}_cv_list_ver` が追加されている。シリアライザはこれを検出し、対応する XML 要素に属性を付加する。

```xml
<!-- 統制語彙なし -->
<collMode>郵送調査</collMode>

<!-- 統制語彙あり -->
<collMode codeListID="ModeOfCollection" codeListVersionID="3.0">
  SelfAdministeredQuestionnaire.Paper
</collMode>
```

#### 4.5.6 主要メソッド

| メソッド | シグネチャ | 説明 |
|---|---|---|
| `to_xml_string(ddi)` | `(dict) → str` | UTF-8 XML 文字列を返す |
| `to_xml(ddi)` | `(dict) → bytes` | UTF-8 XML バイト列を返す |
| `save(ddi, path)` | `(dict, str) → None` | XML ファイルとして保存する |
| `validate_structure(ddi)` | `(dict) → (bool, list[str])` | 構造検証。`(ok, errors)` を返す |

#### 4.5.7 `validate_structure()` の検証項目

| エラー種別 | 検証内容 |
|---|---|
| `MISSING required` | `title` が存在しない |
| `MISSING recommended` | `abstract`, `principal_investigators`, `universe` のいずれかが存在しない |
| `TYPE WARNING` | リスト型フィールド（`keywords` 等）に非リスト値が入っている |
| `INVALID variables` | `variables` の要素が dict でない |

#### 4.5.8 ショートカット関数

```python
from curategpt.wrappers.social.ddi_xml_serializer import to_ddi_xml

to_ddi_xml(ddi, file_path="output.xml")   # 1行で保存
```

---

## 5. データモデル仕様

### 5.1 DDI メタデータ dict の完全フィールド一覧

`PaperToDDIAgent` が返す dict（および `apply_cv_mappings()` 適用後）のフィールド定義。

#### 書誌グループ（bibliographic）

| フィールド名 | 型 | DDI XML 要素 | 説明 |
|---|---|---|---|
| `title` | string | `titl` | 調査の正式名称 |
| `alternative_title` | string \| null | `altTitl` | 副題・略称 |
| `study_ids` | list[string] \| null | `IDNo`（繰り返し） | アーカイブ識別子（例: ZA1234） |
| `principal_investigators` | list[string] \| null | `AuthEnty`（繰り返し） | 研究代表者の氏名リスト |
| `funding_agencies` | list[string] \| null | `fundAg`（繰り返し） | 助成機関名リスト |
| `distributors` | list[string] \| null | `distrbtr`（繰り返し） | 配布機関名リスト |
| `abstract` | string \| null | `abstract` | 調査概要 |
| `keywords` | list[string] \| null | `keyword`（繰り返し） | キーワードリスト |
| `topic_classification` | list[string] \| null | `topcClas`（繰り返し） | 主題分類リスト |

#### 方法論グループ（methodology）

| フィールド名 | 型 | DDI XML 要素 | 説明 |
|---|---|---|---|
| `time_periods` | list[{event, date}] \| null | `timePrd`（繰り返し） | 調査期間（start/end/single） |
| `collection_dates` | list[{event, date}] \| null | `collDate`（繰り返し） | データ収集日 |
| `nations` | list[string] \| null | `nation`（繰り返し） | 対象国 |
| `geographic_coverage` | list[string] \| null | `geogCover`（繰り返し） | 地理的カバレッジ |
| `universe` | string \| null | `universe` | 調査母集団の定義 |
| `analysis_unit` | string \| null | `anlyUnit` | 分析単位（個人・世帯等） |
| `sampling_procedure` | string \| null | `sampProc` | 標本抽出方法 |
| `collection_mode` | string \| null | `collMode` | 調査モード（郵送・面接等） |
| `time_method` | string \| null | `timeMeth` | 時系列種別（横断・縦断等） |
| `collection_situation` | string \| null | `collSitu` | 収集状況 |
| `weighting` | string \| null | `weight` | 重み付け方法 |

#### 変数グループ（survey）

| フィールド名 | 型 | DDI XML 要素 | 説明 |
|---|---|---|---|
| `variables` | list[Variable] | `dataDscr/var`（繰り返し） | 変数定義リスト |

**Variable オブジェクトの構造**:

| キー | 型 | DDI XML 要素 | 説明 |
|---|---|---|---|
| `name` | string | `var[@name]` | 変数コード |
| `label` | string \| null | `labl` | 変数ラベル |
| `question` | string \| null | `qstn/qstnLit` | 質問文 |
| `filter_condition` | string \| null | `qstn/filtrInstr` | スキップ条件（C2） |
| `categories` | list[{value, label}] \| null | `catgry` | 選択肢リスト |
| `format` | string \| null | `varFormat[@type]` | `numeric` / `character` / `date` |

#### 統制語彙追加フィールド（apply_cv_mappings() 適用後）

| フィールド名 | 型 | 説明 |
|---|---|---|
| `{field}_cv` | string \| list[string] \| null | CV コード |
| `{field}_cv_list_id` | string | codeListID 属性値 |
| `{field}_cv_list_ver` | string | codeListVersionID 属性値 |

---

## 6. 設定パラメータ一覧

### 6.1 PaperToDDIAgent のパラメータ

| パラメータ | 型 | デフォルト | 説明 |
|---|---|---|---|
| `knowledge_source` | DBAdapter \| None | None | RAG 知識ベース。None でコールドスタートモード |
| `knowledge_source_collection` | str | `"ddi_examples"` | ChromaDB コレクション名 |
| `extractor` | Extractor | 必須 | LLM ラッパー（BasicExtractor 等） |
| `context_tokens` | int | 8000 | LLM のコンテキストウィンドウ（トークン数） |
| `rag_example_limit` | int | 3 | プロンプトに付加する RAG 事例数 |
| `enable_source_check` | bool | True | Hallucination 原文照合チェック（C1 L3）の有効/無効 |
| `enable_cv_mapping` | bool | True | 統制語彙マッピング（C4）の有効/無効 |
| `enable_table_extraction` | bool | True | PDF テーブル構造抽出（A2）の有効/無効 |
| `enable_survey_promotion` | bool | True | 非構造化調査票の自動昇格（B3）の有効/無効 |
| `vars_per_block` | int | 50 | 1回の LLM 呼び出しで処理する最大変数数（C3） |

### 6.2 DDICodebookWrapper のパラメータ

| パラメータ | 型 | デフォルト | 説明 |
|---|---|---|---|
| `oai_base_url` | str | GESIS_OAI_URL | OAI-PMH エンドポイント |
| `max_records` | int | 200 | 収集上限件数 |
| `metadata_prefix` | str | `"oai_ddi25"` | OAI-PMH メタデータプレフィックス |

### 6.3 推奨設定（用途別）

#### 標準構成（日本語調査票）

```python
agent = PaperToDDIAgent(
    knowledge_source=db,
    knowledge_source_collection="ddi_examples",
    extractor=extractor,          # gpt-4o 推奨
    context_tokens=128_000,       # gpt-4o のコンテキスト
    rag_example_limit=3,
    enable_source_check=True,     # 幻覚対策 有効
    enable_cv_mapping=True,       # 統制語彙 有効
    enable_table_extraction=True, # 表抽出 有効
    enable_survey_promotion=True, # 非構造化調査票対応 有効
    vars_per_block=50,            # 50件ごとにブロック分割
)
```

#### 大規模調査票（300変数以上）

```python
agent = PaperToDDIAgent(
    extractor=extractor,
    context_tokens=128_000,
    vars_per_block=100,           # ブロックサイズを増やして LLM 呼び出し回数を削減
)
```

#### 誤検出が多い場合（特殊文書）

```python
agent = PaperToDDIAgent(
    extractor=extractor,
    enable_source_check=False,    # 原文照合チェックを無効化
    enable_survey_promotion=False,# 自動昇格を無効化（Q1 への言及が多い報告書）
    enable_table_extraction=False,# テーブル検出を無効化（過剰検出の場合）
)
```

---

## 7. エラー処理とフォールバック

| 状況 | フォールバック処理 |
|---|---|
| `pdfplumber` が未インストール | `textract` を試みる。両方なければ空文字列を返す |
| `find_tables()` が例外 | `page.extract_text()` にフォールバック |
| LLM が YAML として解析不能な応答を返す | 空の dict を返し、当該グループはスキップ |
| `survey_items` 節が未検出（構造化文書） | `full` テキスト全文をフォールバックとして使用 |
| `survey_items` 節が未検出（非構造化調査票） | `_promote_survey_items()` で `preamble` を自動昇格（B3） |
| 変数境界が検出されない（分割処理） | テキスト全体を1ブロックとして処理 |
| 原文照合で値が不一致 | WARNING ログを出力して自動 null 化 |
| 統制語彙マッチなし | 元の値を保持し `{field}_cv = None` を設定 |
| `validate_structure()` のエラー | エラーリストを返す。XML 出力自体は実行される |

---

## 8. テスト仕様

### 8.1 テストファイル一覧

| テストファイル | テスト対象 | ケース数 | 結果 |
|---|---|---|---|
| `tests/agents/test_paper_to_ddi_b3.py` | B3 非構造化調査票の自動昇格 | 22 | 22/22 PASSED |
| `tests/agents/test_paper_to_ddi_c3.py` | C3 変数分割抽出 | 26 | 26/26 PASSED |
| `tests/wrappers/test_ddi_xml_serializer.py` | DDIXMLSerializer 基本機能 | — | PASSED |
| `tests/wrappers/test_ddi_xml_serializer_c2.py` | C2 フィルター質問・`<filtrInstr>` 出力 | 13 | 13/13 PASSED |

### 8.2 B3 テスト（22ケース）

| テストクラス | テスト内容 |
|---|---|
| `TestPromoteSurveyItems` | Q/問/F/第N問/パイプ表形式スタイルの昇格、閾値（3件未満は昇格しない）、既存 survey_items があればスキップ、preamble の分割（前文保持・Q1 以降昇格）、`full` キー不変 |
| `TestExtractSectionsB3Integration` | 見出しなし + Q スタイル → survey_items 生成、日本語 問N → 生成、明示的見出し優先、`enable_survey_promotion=False` → 昇格なし、デフォルト True |

### 8.3 C3 テスト（26ケース）

| テストクラス | テスト内容 |
|---|---|
| `TestQuestionBoundaryRe` | Q/F/SQ/問/第N問/パイプ表形式の正マッチ、選択肢行・見出し行の非マッチ（13ケース） |
| `TestSplitSurveyBlocks` | 30問→1ブロック、ちょうど50問→1ブロック、120問→3ブロック、カスタム `vars_per_block`、境界位置の正確性（11ケース） |
| `TestExtractSurveyVariablesChunked` | 単一ブロック→1回呼び出し、3ブロック→3回呼び出し・結合、空レスポンスのスキップ（4ケース） |

### 8.4 C2 テスト（13ケース）

| テストクラス | テスト内容 |
|---|---|
| `TestBuildVarFilterCondition` | `filter_condition` → `<filtrInstr>` 出力、`question` + `filter_condition` の共存、`filter_condition` のみ（`question`=None）、英語スキップ指示、`filter_condition` なし → `<filtrInstr>` なし、両方 None → `<qstn>` 自体なし、`<labl>`・`<catgry>` への影響なし、複数変数での選択的出力（10ケース） |
| `TestSurveyInstructionsFilterCondition` | プロンプトに `filter_condition` 記述あり、スキップ/フィルター例示あり、null 指示あり（3ケース） |

### 8.5 テスト実行方法

依存ライブラリ（pdfplumber, llm 等）を使わずに実行できるよう、
各テストファイルはモジュールスタブを使ってスタンドアロンで動作する。

```bash
# B3 テスト
python -m unittest tests/agents/test_paper_to_ddi_b3.py -v

# C3 テスト
python -m unittest tests/agents/test_paper_to_ddi_c3.py -v

# C2 テスト（XML シリアライザ）
python -m unittest tests/wrappers/test_ddi_xml_serializer_c2.py -v
```

---

## 付録：関連ドキュメント

| ファイル | 内容 |
|---|---|
| `DDI_PIPELINE_COMPLETE.md` | 実装状況・手順・残課題の全体まとめ |
| `DDI_HALLUCINATION_GUARD_SUMMARY.md` | C1 Hallucination 対策の実装詳細 |
| `DDI_FILTER_QUESTION_SUMMARY.md` | C2 フィルター質問対応の実装詳細 |
| `DDI_CHUNKED_SURVEY_SUMMARY.md` | C3 変数分割抽出の実装詳細 |
| `DDI_UNSTRUCTURED_SURVEY_SUMMARY.md` | B3 非構造化調査票対応の実装詳細 |
| `DDI_TABLE_EXTRACTION_SUMMARY.md` | A2 表組みレイアウト崩れ対応の実装詳細 |
| `DDI_MULTI_FILE_SUMMARY.md` | D1 複数ファイル一括処理の実装詳細 |
| `DDI_WRAPPER_SUMMARY.md` | DDICodebookWrapper・FullDocumentWrapper の実装詳細 |
