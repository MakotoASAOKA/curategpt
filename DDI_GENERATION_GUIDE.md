# 調査票・調査報告書から DDI-Codebook を生成する手順と課題

---

## 概要

本ドキュメントは、CurateGPT（および今回追加した DDI パイプライン）を用いて、
社会調査の **調査票（questionnaire）** および **調査報告書（survey report）** から
**DDI-Codebook 2.x** 形式のメタデータを生成する際の手順と課題を整理したものです。

---

## 全体フロー

```
【入力】                    【処理】                       【出力】

調査票 (PDF/DOCX)  ──┐                               ┌── DDI メタデータ (dict)
                     ├──► PaperToDDIAgent ──────────►│
調査報告書 (PDF)   ──┘                               └──► （未実装）DDI XML

OAI-PMH リポジトリ ──► DDICodebookWrapper ──► ChromaDB（RAG ナレッジベース）
                         （GESIS/ICPSR 等）               ↑ RAG 事例として参照
```

---

## 手順

### Step 0：環境準備

#### 必要な依存ライブラリのインストール

```bash
pip install curategpt pdfplumber textract pyyaml
```

| ライブラリ | 用途 | 備考 |
|---|---|---|
| `pdfplumber` | PDF テキスト・レイアウト抽出 | 推奨。日本語 PDF でも比較的安定 |
| `textract` | DOC/DOCX/XLS/PPT 等のテキスト抽出 | OS 依存（poppler, tesseract 等が別途必要） |
| `pyyaml` | LLM 応答の YAML パース | CurateGPT に付属 |

#### API キーの設定

```bash
export OPENAI_API_KEY="sk-..."
# または llm コマンドで設定
llm keys set openai
```

---

### Step 1：DDI 事例の事前収集（ナレッジベース構築）

`PaperToDDIAgent` は RAG（検索拡張生成）により既存の DDI 事例をプロンプトに付加する。
このため、まず国際リポジトリから DDI メタデータを収集し、ChromaDB に格納する。

#### 1-1. GESIS（欧州社会科学）から収集

```bash
curategpt view index -c ddi_examples --view ddi_codebook \
    --init-with "{oai_base_url: 'https://api.gesis.org/DBK/OAI-PMH', max_records: 200}"
```

#### 1-2. SSJDA（日本社会科学）から収集

```bash
curategpt view index -c ddi_examples --view ddi_codebook \
    --init-with "{oai_base_url: 'https://ssjda.iss.u-tokyo.ac.jp/oai', max_records: 200}"
```

#### 1-3. 収集内容の確認

```bash
curategpt view search -c ddi_examples "社会意識 横断調査"
```

> **cold-start への対処：**
> `knowledge_source=None` とすることで RAG なしでも動作するが、
> 事例がある場合と比較して抽出精度は低下する。
> 最低でも同一領域（社会調査 / 意識調査など）の事例を 50 件以上収集することを推奨。

---

### Step 2：入力文書の準備

#### 2-1. ファイル形式の確認

| 形式 | 推奨対応 | 注意点 |
|---|---|---|
| PDF（テキスト埋め込み） | pdfplumber で直接読み込み可 | 2段組・表組みでレイアウト崩れの可能性 |
| PDF（スキャン画像） | OCR 変換が必要（textract + tesseract） | 日本語精度は tesseract の学習データに依存 |
| DOCX / ODT | textract で読み込み可 | 表組み調査票は変換後に構造が失われやすい |
| テキスト / Markdown | open() 直読み | 構造情報が明示されており最も精度が高い |

#### 2-2. 調査票と調査報告書の役割分担

| 文書種別 | 主に含む情報 | 主要な DDI フィールド |
|---|---|---|
| **調査報告書** | 調査概要・研究者名・調査期間・地理的範囲・標本設計・調査方法 | `title`, `abstract`, `principal_investigators`, `time_periods`, `nations`, `sampling_procedure`, `collection_mode`, `universe` |
| **調査票** | 質問文・回答選択肢・変数コード・フィルター条件 | `variables`（各変数の name / label / question / categories） |

> **推奨：** 両ファイルを別々に処理し、後でマージする。

---

### Step 3：DDI メタデータ抽出

#### 3-1. Pythonスクリプトからの実行

```python
from curategpt.store import ChromaDBAdapter
from curategpt.extract.basic_extractor import BasicExtractor
from curategpt.agents.paper_to_ddi_agent import PaperToDDIAgent
import llm

# Step 1 で構築したナレッジベースを RAG ソースとして使用
store = ChromaDBAdapter("~/.curategpt/db")
extractor = BasicExtractor()
extractor.model = llm.get_model("gpt-4o")

agent = PaperToDDIAgent(
    knowledge_source=store,
    knowledge_source_collection="ddi_examples",
    extractor=extractor,
    context_tokens=128_000,   # gpt-4o の場合
    rag_example_limit=3,
)

# 調査報告書からメタデータ抽出
ddi_report = agent.extract_ddi_from_file("survey_report.pdf")

# 調査票から変数情報抽出
ddi_questionnaire = agent.extract_ddi_from_file("questionnaire.pdf")

# 手動マージ（調査票の variables を報告書のメタデータに追加）
ddi_merged = {**ddi_report}
if "variables" in ddi_questionnaire:
    ddi_merged["variables"] = ddi_questionnaire["variables"]
```

#### 3-2. 抽出されるフィールド一覧

| グループ | フィールド名 | 内容例 |
|---|---|---|
| 書誌 | `title` | "2023年社会意識調査" |
| 書誌 | `principal_investigators` | ["山田太郎", "鈴木花子"] |
| 書誌 | `funding_agencies` | ["科学研究費補助金"] |
| 書誌 | `abstract` | 調査の概要説明文 |
| 書誌 | `keywords` | ["社会意識", "格差意識", "パネル調査"] |
| 方法 | `time_periods` | [{"event": "start", "date": "2023-10"}] |
| 方法 | `nations` | ["日本"] |
| 方法 | `geographic_coverage` | ["全国"] |
| 方法 | `universe` | "全国18歳以上の男女" |
| 方法 | `sampling_procedure` | "層化二段無作為抽出法" |
| 方法 | `collection_mode` | "自計式質問紙調査" |
| 変数 | `variables` | [{name, label, question, categories, format}] |

#### 3-3. 抽出内容の確認

```python
import json
print(json.dumps(ddi_merged, ensure_ascii=False, indent=2))

# 変数数の確認
print(f"変数数: {len(ddi_merged.get('variables', []))}")

# 主要フィールドのみ確認
for field in ["title", "principal_investigators", "universe", "sampling_procedure"]:
    print(f"{field}: {ddi_merged.get(field, '（未取得）')}")
```

---

### Step 4：人手による確認・修正

抽出結果に対して以下の観点で確認・修正を行う。

#### 確認チェックリスト

```
[ ] title          — 正式調査名と一致しているか
[ ] principal_investigators — 氏名の表記（姓名順・敬称）が正しいか
[ ] time_periods   — 調査実施期間が正確か（ISO 8601 形式）
[ ] universe       — 調査母集団の定義が正確か
[ ] sampling_procedure — 抽出方法の記述が正確か
[ ] collection_mode — 調査モードが正しい統制語から選ばれているか
[ ] variables      — 変数名・質問文・選択肢コードが正確か
[ ] keywords       — 統制語彙に準拠しているか（後述）
```

#### 統制語彙への対応（手動作業が必要）

| フィールド | 推奨統制語彙 | 備考 |
|---|---|---|
| `topic_classification` | CESSDA Topic Classification | 欧州標準。約30大分類 |
| `collection_mode` | DDI 統制語彙（ICPSR 等準拠） | 例: `Interview.FaceToFace.CAPI` |
| `time_method` | DDI 統制語彙 | 例: `CrossSection`, `Panel`, `Longitudinal` |
| `analysis_unit` | DDI 統制語彙 | 例: `Individual`, `HousingUnit` |

---

### Step 5：DDI XML 出力 ✅ 実装済み

`DDIXMLSerializer` を使用して DDI-Codebook 2.5 準拠 XML を生成できる。

#### DDI XML ファイルへの出力

```python
from curategpt.wrappers.social.ddi_xml_serializer import DDIXMLSerializer, to_ddi_xml

s = DDIXMLSerializer()

# 事前検証（推奨）
ok, errors = s.validate_structure(ddi_merged)
if not ok:
    for e in errors:
        print(f"  警告: {e}")

# XML ファイルとして保存
s.save(ddi_merged, "output_ddi.xml")

# 文字列として取得
xml_str = s.to_xml_string(ddi_merged)

# 1行で済ませる場合
to_ddi_xml(ddi_merged, file_path="output_ddi.xml")
```

#### 外部ツールによる追加編集（任意）

| ツール | 概要 | URL |
|---|---|---|
| NESSTAR Publisher | DDI XML 生成の実績ある専用ツール | nesstar.com |
| DDIEditor | Java ベースの DDI エディタ | ddieditor.sourceforge.net |
| Colectica | 商用 DDI ツール | colectica.com |

> **今後の実装課題：** CurateGPT 内に DDI XML シリアライザを追加することで、
> dict → 有効な DDI 2.x XML への自動変換が可能になる。

---

## 課題一覧

### カテゴリ別課題マップ

```
DDI生成パイプライン の課題
│
├── A. 文書変換層
│    ├── A1. スキャン PDF の OCR 精度
│    ├── A2. 表組み・2段組レイアウト崩れ
│    └── A3. 日本語フォント処理の不安定性
│
├── B. 節検出層
│    ├── ~~B1. 番号付き見出しへの未対応~~ ✅ 実装済み
│    ├── B2. 機関・報告書ごとに異なる見出し表現
│    └── B3. 構造化されていない調査票
│
├── C. LLM 抽出層
│    ├── C1. Hallucination（存在しないフィールドの生成）
│    ├── C2. 変数の枝分かれ構造の非対応
│    ├── C3. トークン上限と変数件数の衝突
│    └── C4. 統制語彙への自動マッピング未対応
│
├── D. 複数文書統合
│    ├── D1. 報告書と調査票の情報マージ
│    └── D2. 情報の矛盾・重複
│
├── E. 出力層
│    ├── E1. DDI XML シリアライザ未実装
│    └── E2. スキーマバリデーション未実装
│
└── F. 品質評価
     ├── F1. 定量評価指標の不在
     └── F2. 人手検証コスト
```

---

### A. 文書変換層の課題

#### A1. スキャン PDF の OCR 精度

| 項目 | 内容 |
|---|---|
| **問題** | 紙調査票をスキャンした PDF はテキストデータを含まず、OCR が必須 |
| **現状** | textract 経由で tesseract を使用するが、日本語の認識精度が不安定 |
| **影響** | 変数名・選択肢コード・質問文の誤認識 → 変数情報が破損 |
| **対策案** | ① Google Cloud Vision / AWS Textract 等の商用 OCR API に切り替える ② 事前に Adobe Acrobat 等でテキスト化した PDF を使用する |

#### A2. 表組み・2段組レイアウト崩れ

| 項目 | 内容 |
|---|---|
| **問題** | 調査票は表形式（質問番号 \| 質問文 \| 選択肢）が多く、pdfplumber でも列順が崩れることがある |
| **影響** | 変数の `categories` の value-label 対応が逆転・欠落 |
| **対策案** | ① pdfplumber の `extract_table()` メソッドで表を構造的に抽出する ② 変換後テキストを目視確認してから処理する |

#### A3. 日本語フォント処理の不安定性

| 項目 | 内容 |
|---|---|
| **問題** | 一部の日本語 PDF（特に古い印刷系フォント）で pdfplumber が文字化けを起こす |
| **対策案** | `prefer_pdfplumber=False` に設定して textract のみ使用する。または MuPDF ベースの pymupdf を追加対応する |

---

### B. 節検出層の課題

#### ~~B1. 番号付き見出しへの未対応~~ ✅ 実装済み

| 項目 | 内容 |
|---|---|
| **問題（解決済み）** | 現行の `SECTION_PATTERNS` は「Methods」「方法」等の純粋な見出し語のみ対応。「Ⅲ 調査方法」「第3章 方法論」「3. 標本設計」等の番号付き見出しを認識できなかった |
| **実装** | `_NUMBERED_HEADING_PREFIX` 正規表現を追加し、`_match_section_heading()` でプレフィックス除去後にパターンマッチを試みる。アラビア数字・ローマ数字・第N章・英字接頭辞に対応 |
| **確認** | 25 ケースのスモークテスト全パス |

#### B2. 機関・報告書ごとに異なる見出し表現

| 項目 | 内容 |
|---|---|
| **問題** | 「調査の概要」「調査設計」「調査の実施」「サーベイデザイン」等、機関によって表現が異なる |
| **影響** | 方法論セクションが検出されず、methodology グループが全文フォールバックになる |
| **対策案** | ① パターン辞書を機関別に拡張する ② 節検出 Pass1 でも LLM を使用し「このドキュメントの目次を抽出してください」と問い合わせる（2段階LLM化） |

#### B3. 構造化されていない調査票

| 項目 | 内容 |
|---|---|
| **問題** | 見出しなしで質問が羅列されている調査票（特に古い調査）では節検出が機能しない |
| **影響** | 全文が "preamble" 扱いになり、変数情報が bibliographic グループで処理される |
| **対策案** | 「Q1.」「問1」等の質問番号パターンを検出して survey_items 節として扱うロジックを追加 |

---

### C. LLM 抽出層の課題

#### C1. Hallucination（幻覚）

| 項目 | 内容 |
|---|---|
| **問題** | 文書に明記されていないフィールドを LLM が推測・生成する |
| **頻発フィールド** | `study_ids`（識別番号の捏造）、`funding_agencies`（記載なし時の推測）、`collection_dates`（曖昧な記述からの誤変換） |
| **対策案** | ① プロンプトに「記載がない場合は必ず null を返すこと。推測しないこと」を強調 ② 生成後に元文書との照合を行う人手チェックを必須化 ③ RAG 事例を増やして根拠ある出力を誘導する |

#### C2. 変数の枝分かれ構造（フィルター質問）の非対応

| 項目 | 内容 |
|---|---|
| **問題** | 調査票には「Q3で『はい』と答えた方のみQ4へ」等のフィルター条件がある。DDI は `<var>` の `@files` 属性や `filtrInstr` 要素でこれを表現できるが、現行の `variables` 抽出はフラットな list しか生成しない |
| **影響** | 枝分かれ構造が失われ、変数間の関係が記録されない |
| **対策案** | `variables` スキーマに `filter_condition` フィールドを追加し、プロンプトで抽出を指示する |

#### C3. トークン上限と変数件数の衝突

| 項目 | 内容 |
|---|---|
| **問題** | 大規模調査（変数 300件以上）では調査票全文がトークン上限を超える。現行は100件上限で打ち切り |
| **影響** | 後半の変数が抽出されない |
| **対策案** | ① 調査票を50〜100変数ごとのブロックに分割して複数回 LLM 呼び出しを行い、結果を結合する ② `context_tokens=128000`（gpt-4o）を設定して上限を拡大する |

#### C4. 統制語彙への自動マッピング未対応

| 項目 | 内容 |
|---|---|
| **問題** | LLM は `collection_mode` に「自計式調査」等の自然言語を返すが、DDI 標準では `Interview.SelfAdministered.Paper` 等のコードが期待される |
| **影響** | メタデータの相互運用性が低下する |
| **対策案** | ① プロンプトに統制語彙リストを付加してLLMに選択させる ② 後処理で文字列マッチングにより統制語コードへ変換する辞書を作成する |

---

### D. 複数文書統合の課題

#### D1. 報告書と調査票のマージ

| 項目 | 内容 |
|---|---|
| **問題** | 現行の `PaperToDDIAgent` は1ファイル入力を前提としており、2ファイルを自動統合する機能がない |
| **影響** | 手動でのマージが必要（`{**ddi_report, "variables": ddi_questionnaire["variables"]}`） |
| **対策案** | `extract_ddi_from_files(files: List[str])` メソッドを追加し、複数ファイルを統合処理する。報告書を書誌・方法論担当、調査票を変数担当として役割分担する |

#### D2. 情報の矛盾・重複

| 項目 | 内容 |
|---|---|
| **問題** | 報告書と調査票で記載内容が微妙に異なる場合（例：調査期間の記述がずれる）にどちらを優先するか判断できない |
| **対策案** | マージ時に矛盾フィールドを検出してログに記録し、人手確認を促すバリデーション処理を追加 |

---

### E. 出力層の課題

#### E1. DDI XML シリアライザ ✅ 実装済み

`src/curategpt/wrappers/social/ddi_xml_serializer.py` として実装完了。

```python
from curategpt.wrappers.social.ddi_xml_serializer import DDIXMLSerializer, to_ddi_xml

s = DDIXMLSerializer()
s.save(ddi_dict, "output.xml")          # ファイル出力
xml_str = s.to_xml_string(ddi_dict)    # 文字列取得
ok, errors = s.validate_structure(ddi_dict)  # 構造検証
```

標準ライブラリ `xml.etree.ElementTree` のみ使用（追加依存なし）。
DDI-Codebook 2.5 名前空間・スキーマロケーション・全フィールドに対応。

#### E2. スキーマバリデーション未実装

| 項目 | 内容 |
|---|---|
| **問題** | 生成した dict / XML が DDI-Codebook 2.5 スキーマに適合するかを検証する手段がない |
| **対策案** | DDI 公式 XSD を用いた `lxml.etree.XMLSchema` によるバリデーションを実装する |

---

### F. 品質評価の課題

#### F1. 定量評価指標の不在

| 項目 | 内容 |
|---|---|
| **問題** | LLM が生成したメタデータの正確性を測る自動評価指標がない |
| **対策案** | ① ゴールドスタンダード（手動作成済みDDI）との比較による Field-level Accuracy を計算する ② ROUGE / BERTScore による抽象的記述フィールドの類似度評価 |

#### F2. 人手検証コスト

| 項目 | 内容 |
|---|---|
| **問題** | 最終的に専門家による確認が必要。特に変数情報（100件超）の検証は高コスト |
| **対策案** | ① 確信度（LLM のログ確率）が低いフィールドを優先的に人手確認するよう出力に付記する ② 確認作業を支援する Streamlit UI（フィールドごとの確認チェックボックス付き）を作成する |

---

## 課題の優先度マトリクス

| 課題 | 対処難易度 | 影響度 | 優先度 |
|---|---|---|---|
| ~~B1. 番号付き見出し未対応~~ | ✅ 実装済み | 高 | **完了** |
| ~~E1. DDI XML シリアライザ~~ | ✅ 実装済み | 高 | **完了** |
| C1. Hallucination 対策 | 低（プロンプト強化） | 高 | 高 |
| D1. 複数文書統合 | 中（メソッド追加） | 高 | 高 |
| C4. 統制語彙マッピング | 中（辞書作成） | 中 | 中 |
| A1. OCR 精度向上 | 高（外部 API 導入） | 中 | 中 |
| C2. 枝分かれ構造対応 | 中（スキーマ拡張） | 中 | 中 |
| C3. 変数件数上限 | 低（分割処理追加） | 中 | 中 |
| E2. スキーマバリデーション | 低（XSD 検証追加） | 低 | 低 |
| F1. 定量評価指標 | 高（評価データ整備） | 低 | 低 |

---

## 実装ロードマップ（案）

```
フェーズ1（即時対応）
├── B1: 番号付き見出しの正規表現拡張 ✅ 実装済み
├── C1: Hallucination 抑制プロンプトの強化
└── C3: 変数の分割処理（100件超対応）

フェーズ2（近期対応）
├── E1: DDI 2.x XML シリアライザの実装 ✅ 実装済み
├── D1: 複数文書統合メソッドの追加
└── C4: 統制語彙マッピング辞書の作成

フェーズ3（中長期対応）
├── B2: LLM を使った動的節検出（2段階化）
├── C2: フィルター質問の枝分かれ構造対応
├── E2: DDI XSD スキーマバリデーション
└── F2: Streamlit UI による確認支援画面
```

---

## 参考：DDI-Codebook 2.5 主要要素と本パイプラインの対応状況

| DDI 要素 | DDI XML パス | 本パイプライン対応 |
|---|---|---|
| 調査タイトル | `stdyDscr/citation/titlStmt/titl` | 対応済み |
| 主任研究者 | `stdyDscr/citation/rspStmt/AuthEnty` | 対応済み |
| 資金提供機関 | `stdyDscr/citation/prodStmt/fundAg` | 対応済み |
| 調査概要 | `stdyDscr/stdyInfo/abstract` | 対応済み |
| キーワード | `stdyDscr/stdyInfo/subject/keyword` | 対応済み |
| 調査対象地域 | `stdyDscr/stdyInfo/sumDscr/geogCover` | 対応済み |
| 調査母集団 | `stdyDscr/stdyInfo/sumDscr/universe` | 対応済み |
| 標本抽出法 | `stdyDscr/method/dataColl/sampProc` | 対応済み |
| データ収集モード | `stdyDscr/method/dataColl/collMode` | 対応済み（統制語なし） |
| 調査実施期間 | `stdyDscr/stdyInfo/sumDscr/collDate` | 対応済み |
| 変数定義 | `dataDscr/var` | 対応済み（枝分かれ未対応） |
| フィルター条件 | `dataDscr/var/filtrInstr` | **未対応** |
| 重み付け情報 | `dataDscr/var/weightVar` | **未対応** |
| DDI XML 出力 | `DDIXMLSerializer` | **✅ 実装済み** |
| スキーマ検証 | DDI 2.5 XSD | **未実装** |
