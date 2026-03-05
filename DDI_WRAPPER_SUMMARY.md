# DDI-Codebook ラッパー 改修内容まとめ

## 変更ファイル一覧

| 操作 | ファイルパス |
|---|---|
| 新規作成 | `src/curategpt/wrappers/social/__init__.py` |
| 新規作成 | `src/curategpt/wrappers/social/ddi_codebook_wrapper.py` |
| 変更 | `src/curategpt/wrappers/__init__.py` |

---

## 1. `wrappers/social/ddi_codebook_wrapper.py` — 本体

**全体構成（644行）：**

```
モジュールトップ
├── リポジトリURL定数（4件）
├── XMLユーティリティ関数（2件）
└── DDICodebookWrapper クラス
    ├── クラス変数・フィールド（設定値）
    ├── BaseWrapper インターフェース（2メソッド）
    ├── OAI-PMH プロトコル層（4メソッド）
    ├── XML → dict 変換層（2メソッド）
    ├── DDI要素パーサー（2メソッド）
    └── LLM補助（1メソッド）
```

---

### A. リポジトリURL定数（19〜22行）

```python
GESIS_OAI_URL = "https://api.gesis.org/DBK/OAI-PMH"      # 欧州社会科学
ICPSR_OAI_URL = "https://www.icpsr.umich.edu/..."         # 米国政治社会科学
UKDS_OAI_URL  = "https://oai.ukdataservice.ac.uk/..."     # 英国
SSJDA_OAI_URL = "https://ssjda.iss.u-tokyo.ac.jp/oai"    # 日本
```

対応するOAI-PMHエンドポイントを定数として定義。`oai_base_url` フィールドに渡すことで任意のリポジトリに切り替え可能。

---

### B. XMLユーティリティ関数（29〜57行）

BioSampleラッパーで必要だったが汎用化されていなかった変換ロジックを独立させたもの。

| 関数 | 役割 |
|---|---|
| `_ensure_list(value)` | xmltodict が子要素1件のときdictを、複数のときlistを返す挙動を統一してlistに正規化 |
| `_get_text(obj)` | 文字列・`{"#text": "..."}` 形式・`None` の3通りのxmltodict出力からテキストを安全に取り出す |

---

### C. `DDICodebookWrapper` クラスフィールド（96〜117行）

| フィールド | デフォルト | 説明 |
|---|---|---|
| `name` | `"ddi_codebook"` | `get_wrapper("ddi_codebook")` で取得するための識別子 |
| `oai_base_url` | GESIS URL | 接続先リポジトリのOAI-PMHエンドポイント |
| `metadata_prefix` | `"oai_ddi"` | DDI形式を指定するOAI-PMHパラメータ（リポジトリにより異なる） |
| `oai_set` | `None` | 特定主題セットに絞り込む場合に指定 |
| `max_records` | `100` | 1回の `external_search()` で収集する最大レコード数 |
| `request_delay` | `0.5` 秒 | サーバー負荷軽減のためのリクエスト間隔 |

---

### D. BaseWrapper インターフェース（123〜163行）

CurateGPTの `BaseWrapper` が要求する2メソッドを実装：

```
external_search(text, expand, limit)
│
├─ expand=True かつ extractor あり → _suggest_set() でOAI-PMHセットを選択
└─ _list_records() を呼び出してバッチ収集
   └─ 結果を BaseWrapper.search() に返す
      └─ BaseWrapper がChromaDBに格納 → ベクトル検索で再ランキング

objects_by_ids(object_ids)
└─ 各IDに対して _get_record() を呼び出し
```

> **BioSampleラッパーとの主な違い：** OAI-PMHはキーワード検索APIを持たないため、
> `external_search()` ではバッチ収集を行い、関連度のランキングはChromaDBのベクトル
> 検索に委ねる設計にした。

---

### E. OAI-PMHプロトコル層（169〜273行）

| メソッド | OAI-PMH動詞 | 役割 |
|---|---|---|
| `_oai_request(params)` | 共通 | HTTPリクエスト送信・xmltodict変換・エラー処理 |
| `_list_records(oai_set, max_records)` | `ListRecords` | 複数レコードを収集。**再開トークン（resumptionToken）** を追跡してページネーション対応 |
| `_get_record(identifier)` | `GetRecord` | 1件のレコードをOAI識別子で取得 |
| `_list_sets()` | `ListSets` | リポジトリが提供するセット一覧を取得（LLMセット提案に使用） |

---

### F. XML → dict 変換層（279〜519行）

```
_parse_oai_record(raw)                OAI-PMHレコード全体を受け取る
│  ├─ 削除済みレコード (@status=deleted) をスキップ
│  └─ <metadata><codeBook> を取り出して objects_from_dict() に渡す
│      ※ 名前空間プレフィックスの揺れ(codeBook/ddi:codeBook/ns0:codeBook)に対応
│
objects_from_dict(codebook, oai_id)   DDI-Codebookのマッピング本体
   ├─ docDscr   → id（コードブックID）
   ├─ stdyDscr/citation → title, alternative_title, study_ids,
   │                       principal_investigators, funding_agencies, distributors
   ├─ stdyDscr/stdyInfo → abstract, keywords, topic_classification,
   │                       time_periods, collection_dates, nations,
   │                       geographic_coverage, universe,
   │                       analysis_unit, sampling_procedure
   ├─ stdyDscr/method   → time_method, collection_mode,
   │                       collection_situation, weighting, cleaning_operations
   ├─ stdyDscr/dataAccs → access_place, access_conditions, special_permissions
   ├─ fileDscr          → file_name, file_type, file_content_description
   └─ dataDscr          → variable_count, variables（_parse_variables経由）
```

#### DDI-Codebook → フラットdict フィールド対応表

| DDI XMLパス | 出力フィールド名 |
|---|---|
| `stdyDscr/citation/titlStmt/titl` | `title` |
| `stdyDscr/citation/titlStmt/altTitl` | `alternative_title` |
| `stdyDscr/citation/titlStmt/IDNo` | `study_ids` |
| `stdyDscr/citation/rspStmt/AuthEnty` | `principal_investigators` |
| `stdyDscr/citation/prodStmt/fundAg` | `funding_agencies` |
| `stdyDscr/citation/distStmt/distrbtr` | `distributors` |
| `stdyDscr/stdyInfo/abstract` | `abstract` |
| `stdyDscr/stdyInfo/subject/keyword` | `keywords` |
| `stdyDscr/stdyInfo/subject/topcClas` | `topic_classification` |
| `stdyDscr/stdyInfo/sumDscr/timePrd` | `time_periods` |
| `stdyDscr/stdyInfo/sumDscr/collDate` | `collection_dates` |
| `stdyDscr/stdyInfo/sumDscr/nation` | `nations` |
| `stdyDscr/stdyInfo/sumDscr/geogCover` | `geographic_coverage` |
| `stdyDscr/stdyInfo/sumDscr/universe` | `universe` |
| `stdyDscr/stdyInfo/sumDscr/anlyUnit` | `analysis_unit` |
| `stdyDscr/stdyInfo/sumDscr/sampProc` | `sampling_procedure` |
| `stdyDscr/method/dataColl/timeMeth` | `time_method` |
| `stdyDscr/method/dataColl/collMode` | `collection_mode` |
| `stdyDscr/method/dataColl/collSitu` | `collection_situation` |
| `stdyDscr/method/dataColl/weight` | `weighting` |
| `stdyDscr/method/dataColl/cleanOps` | `cleaning_operations` |
| `stdyDscr/dataAccs/setAvail/accsPlac` | `access_place` |
| `stdyDscr/dataAccs/useStmt/restrctn` | `access_conditions` |
| `stdyDscr/dataAccs/useStmt/specPerm` | `special_permissions` |
| `fileDscr/fileTxt/fileName` | `file_name` |
| `fileDscr/fileTxt/fileType` | `file_type` |
| `fileDscr/fileTxt/fileCont` | `file_content_description` |
| `dataDscr/var` (件数) | `variable_count` |
| `dataDscr/var` (内容) | `variables` |

---

### G. DDI要素パーサー（525〜606行）

| メソッド | 対象DDI要素 | 出力例 |
|---|---|---|
| `_parse_date_elements()` | `<timePrd>`, `<collDate>` | `[{"event": "start", "date": "2010-09", "label": "September 2010"}]` |
| `_parse_variables()` | `<var>` | 変数ごとに name・label・question・categories・format を抽出。上限200件でトークン溢れを防止 |

`_parse_variables()` が生成する変数エントリの例：

```python
{
    "name": "SEX",
    "label": "性別",
    "question": "あなたの性別を教えてください。",
    "categories": [
        {"value": "1", "label": "男性"},
        {"value": "2", "label": "女性"}
    ],
    "format": "numeric"
}
```

---

### H. LLM補助（613〜643行）

```
_suggest_set(text)
├─ _list_sets() でリポジトリのセット一覧を取得（OAI-PMH ListSets）
├─ LLMに「研究トピック → 最適なsetSpec」を選ばせるプロンプトを発行
└─ 返されたsetSpecを _list_records() に渡して取得対象を絞り込む
```

OAI-PMHにキーワード検索がない制約を、LLMによるセット選択で補完する工夫。

---

## 2. `wrappers/__init__.py` — 変更箇所

```python
# __all__ リストに追加
"DDICodebookWrapper",

# get_wrapper() 関数内に import を追加
from curategpt.wrappers.social.ddi_codebook_wrapper import \
    DDICodebookWrapper  # noqa
```

これにより `get_wrapper("ddi_codebook")` でインスタンスが取得でき、
CLIの `--view ddi_codebook` オプションが機能するようになります。

---

## 使い方

### CLIから

```bash
# GESISからDDIメタデータをインデックス化（最大100件）
curategpt view index -c ddi_gesis --view ddi_codebook \
    --init-with "{oai_base_url: 'https://api.gesis.org/DBK/OAI-PMH'}"

# 日本（SSJDA）からインデックス化
curategpt view index -c ddi_ssjda --view ddi_codebook \
    --init-with "{oai_base_url: 'https://ssjda.iss.u-tokyo.ac.jp/oai'}"

# インデックスに対してRAGチャット
curategpt ask -c ddi_gesis "収入格差に関するパネル調査はありますか？"

# 検索のみ
curategpt view search -c ddi_gesis "income inequality longitudinal"
```

### Pythonから

```python
from curategpt.wrappers.social.ddi_codebook_wrapper import (
    DDICodebookWrapper,
    GESIS_OAI_URL,
    ICPSR_OAI_URL,
    UKDS_OAI_URL,
    SSJDA_OAI_URL,
)

# GESIS（欧州）
wrapper = DDICodebookWrapper(oai_base_url=GESIS_OAI_URL)

# SSJDA（日本）、特定セットに絞り込む例
wrapper = DDICodebookWrapper(
    oai_base_url=SSJDA_OAI_URL,
    metadata_prefix="oai_ddi",
    oai_set="社会意識",
    max_records=50,
)

# ベクトル検索
for obj, score, meta in wrapper.search("income inequality panel survey"):
    print(obj["title"])
    print(obj.get("abstract", "")[:200])
    print(f"  変数数: {obj.get('variable_count', 'N/A')}")
    print(f"  調査期間: {obj.get('time_periods')}")
```

---

## BioSampleラッパーとの設計比較

| 項目 | NCBIBiosampleWrapper | DDICodebookWrapper |
|---|---|---|
| プロトコル | NCBI E-Utilities REST API | OAI-PMH |
| XML取得 | `efetch` verb (HTTP GET) | `ListRecords` / `GetRecord` verb |
| XML変換 | `xmltodict.parse()` | `xmltodict.parse()`（共通） |
| キーワード検索 | NCBI esearch API で事前絞り込み | なし → ChromaDBベクトル検索に委ねる |
| セット選択 | N/A | LLM (`_suggest_set()`) で補完 |
| ページネーション | `retmax` パラメータ | `resumptionToken` を追跡 |
| 変数情報 | なし | `dataDscr/var` を最大200件抽出 |
| 対応リポジトリ | NCBI固定 | `oai_base_url` で任意に切替可能 |
