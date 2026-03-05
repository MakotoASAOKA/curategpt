# 論文全文パイプライン 実装まとめ

チャンク化によるコンテキスト損失の対処として追加した実装の記録。

---

## 解決した課題

| 課題 | 原因 | 影響 |
|---|---|---|
| コンテキスト損失 | `FilesystemWrapper` が文書を **3,000文字ごと** に機械分割する | 方法節が表題・著者情報を参照できない／調査票と本文が切り離される |
| DDIフィールドの欠落 | チャンクをベクトル検索で取得するため、特定節（方法論・調査票）のテキストが別チャンクに分散 | `sampling_procedure`・`variables` 等が抽出されない |
| トークン制限 | `tokens.py` の `estimate_num_tokens()` が gpt-4 系以外で `NotImplementedError` | 他モデル使用時に処理が停止する |

---

## 変更ファイル一覧

| 操作 | ファイルパス |
|---|---|
| 新規作成 | `src/curategpt/wrappers/social/full_document_wrapper.py` |
| 新規作成 | `src/curategpt/agents/paper_to_ddi_agent.py` |
| 変更 | `src/curategpt/wrappers/__init__.py` |

---

## 1. `full_document_wrapper.py` — チャンクなしファイル読み込みラッパー

### 概要

`FilesystemWrapper` の代替として、ファイルを **1件まるごと** ベクトルストアに格納する。
`split_objects()` を呼ばないため、文書全体のコンテキストが保持される。

### クラス定義

```python
@dataclass
class FullDocumentWrapper(BaseWrapper):
    name: ClassVar[str] = "full_document"

    root_directory: Optional[str] = None   # スキャン対象ディレクトリ
    glob: Optional[str] = None             # glob パターン（例: "*.pdf"）
    skip_unprocessable: bool = True        # 変換失敗ファイルをスキップするか
    prefer_pdfplumber: bool = True         # PDF に pdfplumber を優先するか
    max_text_length: int = 200_000         # 親クラスの 3,000 を上書き
```

### FilesystemWrapper との比較

| 項目 | FilesystemWrapper | FullDocumentWrapper |
|---|---|---|
| 分割 | `split_objects()` で 3,000文字ごと | **なし**（1ファイル = 1レコード） |
| PDF抽出 | textract のみ | pdfplumber → textract フォールバック |
| `max_text_length` | 3,000 | 200,000 |
| 用途 | 全文検索向け | `PaperToDDIAgent` の入力専用 |

### メソッド構成

```
FullDocumentWrapper
├── objects(collection, object_ids)     ← CLI "view index" から呼ばれるエントリポイント
├── objects_by_ids(object_ids)          ← 特定ファイルパスによる取得
├── _file_to_object(file_path)          ← 1ファイル → dict 変換
└── _read_file(file_path)               ← テキスト抽出ディスパッチャー
    ├── 平文 (.txt/.md/.py 等) → open() 直読み
    ├── PDF → _read_pdf_pdfplumber()
    │         ↓ 失敗時
    └── その他 → _read_file_textract()
```

### 出力レコード形式

```python
{
    "id":     "/path/to/survey_paper.pdf",   # ファイルパス（一意キー）
    "name":   "survey_paper.pdf",            # ファイル名
    "text":   "...(全文テキスト)...",         # チャンクなし
    "parent": "/path/to",                    # 親ディレクトリ
    "size":   1234567,                        # バイト数
    "mtime":  1709500000.0,                  # 更新タイムスタンプ
}
```

### 対応フォーマット

| 拡張子 | 読み込み方法 |
|---|---|
| `.txt` `.md` `.py` `.csv` `.tsv` `.json` `.yaml` `.yml` | `open()` 直読み（UTF-8） |
| `.pdf` | pdfplumber（レイアウト保持）→ textract フォールバック |
| `.docx` `.xlsx` `.pptx` 等 | textract |

---

## 2. `paper_to_ddi_agent.py` — セクション認識型2パス DDI 抽出エージェント

### 概要

論文・調査票 PDF から DDI-Codebook メタデータを抽出する専用エージェント。
「節検出」→「節グループ単位のLLM呼び出し」という2パス構成で、コンテキスト損失を回避する。

### クラス定義

```python
@dataclass
class PaperToDDIAgent(BaseAgent):
    name: ClassVar[str] = "paper_to_ddi"

    context_tokens: int = 8000    # LLM コンテキストウィンドウ推定値
    rag_example_limit: int = 3    # RAG で取得するDDI事例の最大件数
```

---

### 処理フロー

```
extract_ddi_from_file(file_path)
│
├─① _read_full_document()          ← pdfplumber / textract で全文取得
│
└─ extract_ddi_from_text(text)
   │
   ├─② _extract_sections(text)     ← Pass 1: 節検出
   │    └─ 正規表現で見出し行を判定 → {節名: テキスト} の dict を生成
   │
   └─ 抽出グループ × 3 を順次処理
      │
      ├─③ _build_section_text()    ← 対象節を結合・トークン予算内にトリミング
      │
      ├─④ _build_rag_examples()    ← ChromaDB から類似 DDI 事例を取得（YAML形式）
      │
      ├─⑤ _extract_group()        ← Pass 2: LLM 呼び出し → YAML レスポンス取得
      │    └─ _parse_yaml_response() ← マークダウンフェンス除去 + YAML パース
      │
      └─⑥ _merge_extractions()    ← グループ結果をマージ（リスト重複排除）
```

---

### Pass 1：節検出（SECTION_PATTERNS）

見出し行を1行単位で正規表現照合し、節名を確定する。

| 節名 | 英語パターン例 | 日本語パターン例 |
|---|---|---|
| `abstract` | `Abstract`, `Summary` | `要旨`, `概要`, `抄録` |
| `introduction` | `Introduction`, `Background` | `はじめに`, `序論`, `背景` |
| `methods` | `Methods`, `Methodology`, `Research Design` | `方法`, `調査方法`, `研究デザイン` |
| `participants` | `Participants`, `Sample`, `Respondents` | `対象者`, `調査対象`, `サンプル` |
| `data_collection` | `Data Collection`, `Fieldwork` | `データ収集`, `調査実施` |
| `survey_items` | `Questionnaire`, `Survey Items`, `Appendix A` | `調査票`, `質問票`, `付録` |
| `results` | `Results`, `Findings` | `結果`, `分析結果` |
| `discussion` | `Discussion`, `Conclusion` | `考察`, `結論` |
| `references` | `References`, `Bibliography` | `参考文献`, `引用文献` |

**フォールバック：** 節が検出できなかった場合、全文テキストをトークン予算内にトリミングして使用。

**スキップ節：** `results`, `discussion`, `references` はDDI抽出に不要なため除外。

---

### Pass 2：抽出グループ（EXTRACTION_GROUPS）

LLM 呼び出しを3グループに分割し、節テキストとRAG事例を組み合わせてプロンプトを構築する。

#### グループ1: `bibliographic`（書誌情報）

| 使用節 | 抽出フィールド |
|---|---|
| `preamble`, `abstract`, `introduction` | `title`, `alternative_title`, `abstract`, `principal_investigators`, `funding_agencies`, `distributors`, `study_ids`, `keywords`, `topic_classification` |

#### グループ2: `methodology`（調査方法論）

| 使用節 | 抽出フィールド |
|---|---|
| `methods`, `participants`, `data_collection` | `time_periods`, `collection_dates`, `nations`, `geographic_coverage`, `universe`, `analysis_unit`, `sampling_procedure`, `collection_mode`, `time_method`, `collection_situation`, `weighting` |

#### グループ3: `survey`（調査票変数）

| 使用節 | 抽出フィールド |
|---|---|
| `survey_items` | `variables`（最大100件） |

`variables` の各エントリ形式：
```yaml
variables:
  - name: Q1
    label: 性別
    question: あなたの性別を教えてください。
    categories:
      - value: "1"
        label: 男性
      - value: "2"
        label: 女性
    format: numeric
```

---

### トークン予算管理

```python
# 文字数 ÷ 4 でトークン数を近似（英日混在テキスト向け）
_CHARS_PER_TOKEN = 4
_RESERVED_TOKENS = 1500   # プロンプトオーバーヘッド + RAG事例 + 応答分
_DEFAULT_CONTEXT_TOKENS = 8000

budget_chars = (context_tokens - _RESERVED_TOKENS) * _CHARS_PER_TOKEN
# → デフォルト: (8000 - 1500) × 4 = 26,000 文字
```

`tokens.py` の `estimate_num_tokens()` は gpt-4 系以外で `NotImplementedError` を送出するため、
文字数近似を採用してモデル非依存の動作を保証している。

---

### RAG（検索拡張生成）

```
_build_rag_examples(query_text, target_fields)
├─ knowledge_source.search(query_text, limit=rag_example_limit)
│   └─ ChromaDB でベクトル類似検索
├─ 取得した DDI レコードから target_fields に含まれるフィールドのみ抽出
└─ YAML 形式でシリアライズ → プロンプトに付加
```

**cold-start 対応：** `knowledge_source` が `None` のときは RAG なしで動作（`rag_blocks = []`）。
事前に `DDICodebookWrapper.objects()` でDDI事例を収集しておくことを推奨。

---

### `_merge_extractions()` — グループ結果のマージルール

| フィールド型 | マージ規則 |
|---|---|
| スカラー（str, int 等） | 先に抽出されたグループの値を優先（上書きなし） |
| リスト（keywords 等） | 全グループの値を結合し、`repr()` 一致による重複排除 |
| `None` | 無視（既存値を保持） |

---

## 3. `wrappers/__init__.py` — 変更箇所

```python
# __all__ リストに追加
"FullDocumentWrapper",

# get_wrapper() 関数内に import を追加
from curategpt.wrappers.social.full_document_wrapper import \
    FullDocumentWrapper  # noqa
```

これにより `get_wrapper("full_document")` および CLI の `--view full_document` が機能する。

---

## 使い方

### Python API から

```python
from curategpt.store import ChromaDBAdapter
from curategpt.extract.basic_extractor import BasicExtractor
from curategpt.agents.paper_to_ddi_agent import PaperToDDIAgent
import llm

# ① DDI 知識ベースを用意（事前に DDICodebookWrapper で収集済み）
store = ChromaDBAdapter("~/.curategpt/db")
extractor = BasicExtractor()
extractor.model = llm.get_model("gpt-4o")

# ② エージェント初期化
agent = PaperToDDIAgent(
    knowledge_source=store,
    knowledge_source_collection="ddi_gesis",  # RAG ソース
    extractor=extractor,
    context_tokens=128_000,   # gpt-4o の場合
    rag_example_limit=3,
)

# ③ PDF から DDI メタデータを抽出
ddi = agent.extract_ddi_from_file("survey_paper.pdf")

# ④ 結果確認
print(ddi["title"])
print(ddi.get("abstract", "")[:200])
print(ddi.get("sampling_procedure"))
print(f"変数数: {len(ddi.get('variables', []))}")
```

### CLIから（FullDocumentWrapper でインデックス化）

```bash
# PDF をチャンクなしでインデックス化
curategpt view index -c papers_full --view full_document \
    --init-with "{root_directory: './papers', glob: '*.pdf'}"

# テキスト検索
curategpt view search -c papers_full "サンプリング方法 層化抽出"
```

### RAG なし（cold-start）でのシングルファイル抽出

```python
# knowledge_source を None にすると RAG なしで動作
agent = PaperToDDIAgent(
    knowledge_source=None,
    extractor=extractor,
)
ddi = agent.extract_ddi_from_file("questionnaire.pdf")
```

---

## FilesystemWrapper との設計比較

| 項目 | FilesystemWrapper | FullDocumentWrapper + PaperToDDIAgent |
|---|---|---|
| 文書分割 | 3,000文字ごと機械分割 | 節見出し（regex）で論理分割 |
| LLM への入力単位 | チャンク（文脈なし） | 節グループ（関連節をまとめて渡す） |
| トークン管理 | `split_objects()` で制御 | `_char_budget()` で文字数換算 |
| RAG | ChromaDB 検索結果をそのまま使用 | DDI 事例の関連フィールドのみ抽出してYAML化 |
| 日本語対応 | なし | 節見出し正規表現に日本語パターンを追加 |
| cold-start | N/A | `knowledge_source=None` で RAG なし動作 |

---

## 依存ライブラリ

| ライブラリ | 用途 | インストール |
|---|---|---|
| `pdfplumber` | PDF テキスト抽出（推奨） | `pip install pdfplumber` |
| `textract` | バイナリ形式全般のテキスト抽出 | `pip install textract` |
| `pyyaml` | LLM 応答の YAML パース | CurateGPT 依存に含まれる |
