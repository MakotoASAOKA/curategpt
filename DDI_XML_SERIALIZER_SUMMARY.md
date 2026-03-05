# DDI XML シリアライザ 実装まとめ

DDI-Codebook 2.5 XML 出力機能の実装記録。

---

## 変更ファイル一覧

| 操作 | ファイルパス |
|---|---|
| 新規作成 | `src/curategpt/wrappers/social/ddi_xml_serializer.py` |
| 新規作成 | `tests/wrappers/test_ddi_xml_serializer.py` |

---

## 解決した課題

| 課題 | 解決内容 |
|---|---|
| **DDI XML 出力が存在しなかった** | `DDICodebookWrapper` / `PaperToDDIAgent` が生成する Python dict を DDI-Codebook 2.5 XML に変換するシリアライザを新規実装 |
| **追加依存なし** | 標準ライブラリ `xml.etree.ElementTree` のみ使用（`lxml` 等の追加インストール不要） |
| **名前空間の整合性** | `ET.register_namespace("", DDI_NS)` により `xmlns="ddi:codebook:2_5"` をデフォルト名前空間として出力 |
| **不正な XML ID** | OAI-PMH 識別子（例: `oai:gesis.org:ZA2800`）を XML NCName に安全変換（`_sanitise_id()`） |
| **型の不一致** | `"integer"`, `"float"` → `"numeric"` 等の型名正規化（`_normalise_format()`） |

---

## 1. `ddi_xml_serializer.py` — 本体

### モジュール構成（645行）

```
ddi_xml_serializer.py
│
├── 名前空間定数（3件）
│    ├── DDI_NS  = "ddi:codebook:2_5"
│    ├── XSI_NS  = "http://www.w3.org/2001/XMLSchema-instance"
│    └── DDI_SCHEMA_LOCATION
│
├── ヘルパー関数（3件）
│    ├── _q(local)              Clark記法変換 "{namespace}localname"
│    ├── _ensure_list(value)    scalar/list/None → list に正規化
│    └── _str_or_none(value)    空文字・None を None に統一
│    └── _sub(parent, tag, ...) DDI名前空間の SubElement を生成
│
├── to_ddi_xml()               クラスを使わず1行で呼べる便利関数
│
├── DDIXMLSerializer クラス
│    ├── パブリックインターフェース（4メソッド）
│    ├── ツリービルダー（8メソッド）
│    └── ユーティリティ（モジュールレベル関数）
│
└── プライベートユーティリティ
     ├── _normalise_format()    型名正規化
     └── _sanitise_id()         XML ID 安全化
```

---

### クラス定義

```python
class DDIXMLSerializer:
    def __init__(self, ddi_version: str = "2.5")
```

| パラメータ | デフォルト | 説明 |
|---|---|---|
| `ddi_version` | `"2.5"` | `<codeBook version="...">` に出力するバージョン文字列。`"2.1"` 等に変更可能 |

---

### パブリックインターフェース

| メソッド / 関数 | 戻り値 | 説明 |
|---|---|---|
| `s.to_xml(ddi_dict)` | `bytes` | UTF-8 バイト列（`<?xml ...?>` 宣言付き） |
| `s.to_xml_string(ddi_dict)` | `str` | UTF-8 文字列（`to_xml()` のデコード版） |
| `s.save(ddi_dict, path)` | `None` | XML ファイルを書き出す。親ディレクトリが存在しない場合は自動作成 |
| `s.validate_structure(ddi_dict)` | `(bool, List[str])` | 必須・推奨フィールドの構造検証。`(ok, errors)` を返す |
| `to_ddi_xml(ddi_dict, file_path, version)` | `str` | クラスを介さず使えるショートカット関数 |

---

### 生成される DDI XML の構造

```xml
<?xml version='1.0' encoding='UTF-8'?>
<codeBook xmlns="ddi:codebook:2_5"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
          xsi:schemaLocation="ddi:codebook:2_5
            http://www.ddialliance.org/Specification/DDI-Codebook/2.5/XMLSchema/codebook.xsd"
          version="2.5" ID="test-001">

  <docDscr>                          <!-- コードブック文書自体の情報 -->
    <citation>
      <titlStmt>
        <titl>Codebook for: テスト社会調査 2024</titl>
      </titlStmt>
      <prodStmt>
        <prodDate>2026-03-04</prodDate>   <!-- 生成日（実行日付を自動設定）-->
      </prodStmt>
    </citation>
  </docDscr>

  <stdyDscr>                         <!-- 調査研究の記述 -->
    <citation>
      <titlStmt>
        <titl>テスト社会調査 2024</titl>
        <altTitl>...</altTitl>            <!-- 任意 -->
        <IDNo>ZA2800</IDNo>              <!-- 繰り返し可 -->
      </titlStmt>
      <rspStmt>
        <AuthEnty>山田太郎</AuthEnty>     <!-- 繰り返し可 -->
      </rspStmt>
      <prodStmt>
        <fundAg>科学研究費補助金</fundAg>  <!-- 繰り返し可 -->
      </prodStmt>
      <distStmt>
        <distrbtr>...</distrbtr>          <!-- 繰り返し可 -->
      </distStmt>
    </citation>
    <stdyInfo>
      <subject>
        <keyword>社会意識</keyword>        <!-- 繰り返し可 -->
        <topcClas>社会・文化</topcClas>    <!-- 繰り返し可 -->
      </subject>
      <abstract>本調査は…</abstract>
      <sumDscr>
        <timePrd event="start" date="2024-01">…</timePrd>  <!-- 繰り返し可 -->
        <collDate event="start" date="2024-01-15">…</collDate>
        <nation>日本</nation>
        <geogCover>全国</geogCover>
        <universe>全国18歳以上の男女</universe>
        <anlyUnit>個人</anlyUnit>
      </sumDscr>
    </stdyInfo>
    <method>
      <dataColl>
        <timeMeth>Cross-section</timeMeth>
        <sampProc>層化二段無作為抽出法</sampProc>
        <collMode>自計式質問紙調査（郵送）</collMode>
        <collSitu>…</collSitu>
        <weight>…</weight>
        <cleanOps>…</cleanOps>
      </dataColl>
    </method>
    <dataAccs>
      <setAvail><accsPlac>…</accsPlac></setAvail>
      <useStmt>
        <restrctn>…</restrctn>
        <specPerm>…</specPerm>
      </useStmt>
    </dataAccs>
  </stdyDscr>

  <fileDscr>                         <!-- データファイル情報 -->
    <fileTxt>
      <fileName>survey2024.dta</fileName>
      <fileType>Stata</fileType>
      <fileCont>…</fileCont>
    </fileTxt>
  </fileDscr>

  <dataDscr>                         <!-- 変数定義 -->
    <var ID="V1" name="SEX">
      <labl>性別</labl>
      <qstn>
        <qstnLit>あなたの性別を教えてください。</qstnLit>
      </qstn>
      <catgry>
        <catValu>1</catValu>
        <labl>男性</labl>
      </catgry>
      <catgry>
        <catValu>2</catValu>
        <labl>女性</labl>
      </catgry>
      <varFormat type="numeric" />
    </var>
  </dataDscr>

</codeBook>
```

---

### フィールド対応表（dict キー → DDI XML パス）

| dict キー | DDI XML パス | 繰り返し |
|---|---|---|
| `id` | `codeBook[@ID]` | — |
| `title` | `stdyDscr/citation/titlStmt/titl` | — |
| `alternative_title` | `stdyDscr/citation/titlStmt/altTitl` | — |
| `study_ids` | `stdyDscr/citation/titlStmt/IDNo` | ✓ |
| `principal_investigators` | `stdyDscr/citation/rspStmt/AuthEnty` | ✓ |
| `funding_agencies` | `stdyDscr/citation/prodStmt/fundAg` | ✓ |
| `distributors` | `stdyDscr/citation/distStmt/distrbtr` | ✓ |
| `abstract` | `stdyDscr/stdyInfo/abstract` | — |
| `keywords` | `stdyDscr/stdyInfo/subject/keyword` | ✓ |
| `topic_classification` | `stdyDscr/stdyInfo/subject/topcClas` | ✓ |
| `time_periods` | `stdyDscr/stdyInfo/sumDscr/timePrd` | ✓ |
| `collection_dates` | `stdyDscr/stdyInfo/sumDscr/collDate` | ✓ |
| `nations` | `stdyDscr/stdyInfo/sumDscr/nation` | ✓ |
| `geographic_coverage` | `stdyDscr/stdyInfo/sumDscr/geogCover` | ✓ |
| `universe` | `stdyDscr/stdyInfo/sumDscr/universe` | — |
| `analysis_unit` | `stdyDscr/stdyInfo/sumDscr/anlyUnit` | — |
| `time_method` | `stdyDscr/method/dataColl/timeMeth` | — |
| `sampling_procedure` | `stdyDscr/method/dataColl/sampProc` | — |
| `collection_mode` | `stdyDscr/method/dataColl/collMode` | — |
| `collection_situation` | `stdyDscr/method/dataColl/collSitu` | — |
| `weighting` | `stdyDscr/method/dataColl/weight` | — |
| `cleaning_operations` | `stdyDscr/method/dataColl/cleanOps` | — |
| `access_place` | `stdyDscr/dataAccs/setAvail/accsPlac` | — |
| `access_conditions` | `stdyDscr/dataAccs/useStmt/restrctn` | — |
| `special_permissions` | `stdyDscr/dataAccs/useStmt/specPerm` | — |
| `file_name` | `fileDscr/fileTxt/fileName` | — |
| `file_type` | `fileDscr/fileTxt/fileType` | — |
| `file_content_description` | `fileDscr/fileTxt/fileCont` | — |
| `variables` | `dataDscr/var`（各変数を以下で展開） | ✓ |
| `variables[].name` | `dataDscr/var[@name]` | — |
| `variables[].label` | `dataDscr/var/labl` | — |
| `variables[].question` | `dataDscr/var/qstn/qstnLit` | — |
| `variables[].categories[].value` | `dataDscr/var/catgry/catValu` | ✓ |
| `variables[].categories[].label` | `dataDscr/var/catgry/labl` | ✓ |
| `variables[].format` | `dataDscr/var/varFormat[@type]` | — |

---

### 日付要素の入力形式

`time_periods` / `collection_dates` の各要素には **文字列** または **辞書** を指定できる。

```python
# 辞書形式（推奨）
{"event": "start", "date": "2024-01", "label": "2024年1月"}
# → <timePrd event="start" date="2024-01">2024年1月</timePrd>

# 文字列形式（シンプル）
"2024-01"
# → <timePrd>2024-01</timePrd>
```

| 辞書キー | DDI 属性・内容 | 備考 |
|---|---|---|
| `event` | `timePrd[@event]` | `start` / `end` / `single` |
| `date` | `timePrd[@date]` | ISO 8601 推奨（`YYYY-MM-DD` 等） |
| `label` | `timePrd` のテキスト内容 | 省略時は `date` の値をそのまま使用 |

---

### 変数フォーマット正規化（`_normalise_format()`）

LLM が出力する自由記述の型名を DDI の `varFormat[@type]` 統制値に変換する。

| 入力（LLM出力） | DDI出力 |
|---|---|
| `numeric`, `number`, `int`, `integer`, `float`, `double`, `decimal` | `numeric` |
| `string`, `text`, `character`, `char` | `character` |
| `date`, `datetime`, `timestamp` | `date` |
| それ以外（`binary` 等） | そのまま通過 |

---

### ID サニタイズ（`_sanitise_id()`）

XML の `ID` 属性は NCName（名前空間修飾なし名前）でなければならない。
OAI-PMH 識別子はコロン・スペースを含むため変換が必要。

| 入力例 | 出力例 |
|---|---|
| `ZA2800` | `ZA2800` |
| `oai:gesis.org:ZA2800` | `oai-gesis.org-ZA2800` |
| `my study 001` | `my-study-001` |
| `123abc`（数字始まり） | `id-123abc` |
| `""`（空文字） | `ddi-unknown` |

---

### `validate_structure()` の検証内容

```python
ok, errors = s.validate_structure(ddi_dict)
```

| 検証種別 | 対象フィールド | エラーメッセージ例 |
|---|---|---|
| **必須チェック** | `title` | `"MISSING required field: 'title'"` |
| **推奨チェック** | `abstract`, `principal_investigators`, `universe` | `"MISSING recommended field: 'abstract'"` |
| **型チェック** | リスト型フィールド全般 | `"TYPE WARNING: 'keywords' should be a list, got int"` |
| **変数構造チェック** | `variables` の各要素 | `"INVALID variables[2]: expected dict, got str"` |
| **変数名チェック** | `variables[].name` / `variables[].label` | `"WARNING variables[3]: neither 'name' nor 'label' provided"` |

---

## 2. `test_ddi_xml_serializer.py` — テストスイート

### テスト構成（45テスト）

| テストクラス / グループ | 件数 | 確認内容 |
|---|---|---|
| `TestDDIXMLSerializerMinimal` | 8 | 最小構成 dict での基本出力確認 |
| `TestDDIXMLSerializerFull` | 22 | 全フィールドを含む完全 dict での出力確認 |
| ファイル出力テスト | 4 | `to_xml()` → bytes, `to_xml_string()` → str, `save()` → ファイル作成, 親ディレクトリ自動作成 |
| `validate_structure()` テスト | 4 | title 欠落 / 型エラー / 変数構造不正 |
| ヘルパーユーティリティテスト | 6 | `_normalise_format()` と `_sanitise_id()` の全ケース |
| XML 整合性テスト | 1 | `ET.fromstring()` で正常パースできることを確認 |

### テストデータ

```python
# 最小 dict（4フィールド）
DDI_MINIMAL = {
    "id": "test-001",
    "title": "テスト社会調査 2024",
    "abstract": "本調査は日本全国の成人を対象とした意識調査である。",
    "principal_investigators": ["山田太郎", "鈴木花子"],
}

# 完全 dict（全28フィールド + 変数3件）
DDI_FULL = {
    "id": "oai:gesis.org:ZA2800",
    "title": "ALLBUS 1980",
    "alternative_title": "German General Social Survey 1980",
    "study_ids": ["ZA2800", "ICPSR-1234"],
    ...  # 全フィールド
    "variables": [
        {"name": "SEX", "label": "性別", "format": "numeric", ...},
        {"name": "AGE", "label": "年齢", "format": "integer", ...},
        {"name": "COMMENT", "label": "自由回答", "format": "string", ...},
    ]
}
```

### 実行方法

```bash
# 外部依存なし（ネットワーク・OpenAI API キー不要）
pytest tests/wrappers/test_ddi_xml_serializer.py -v
```

---

## 使い方

### 基本的な使い方

```python
from curategpt.wrappers.social.ddi_xml_serializer import DDIXMLSerializer

s = DDIXMLSerializer()

# XML 文字列として取得
xml_str = s.to_xml_string(ddi_dict)

# XML ファイルとして保存
s.save(ddi_dict, "output/study_ZA2800.xml")

# 事前検証（推奨）
ok, errors = s.validate_structure(ddi_dict)
if not ok:
    for e in errors:
        print(f"  警告: {e}")
```

### 1行で済ませる場合

```python
from curategpt.wrappers.social.ddi_xml_serializer import to_ddi_xml

# 文字列取得のみ
xml_str = to_ddi_xml(ddi_dict)

# ファイル保存と文字列取得を同時に
xml_str = to_ddi_xml(ddi_dict, file_path="output.xml")
```

### PaperToDDIAgent と組み合わせた完全フロー

```python
from curategpt.agents.paper_to_ddi_agent import PaperToDDIAgent
from curategpt.wrappers.social.ddi_xml_serializer import DDIXMLSerializer, to_ddi_xml
from curategpt.extract.basic_extractor import BasicExtractor
import llm

# ① LLM でメタデータ抽出
extractor = BasicExtractor()
extractor.model = llm.get_model("gpt-4o")
agent = PaperToDDIAgent(extractor=extractor)

ddi_report = agent.extract_ddi_from_file("survey_report.pdf")
ddi_questionnaire = agent.extract_ddi_from_file("questionnaire.pdf")

# ② 調査票の変数情報をマージ
ddi_merged = {**ddi_report}
if "variables" in ddi_questionnaire:
    ddi_merged["variables"] = ddi_questionnaire["variables"]

# ③ 構造検証
s = DDIXMLSerializer()
ok, errors = s.validate_structure(ddi_merged)
for e in errors:
    print(f"  警告: {e}")

# ④ DDI XML 出力
s.save(ddi_merged, "output/codebook.xml")
print("DDI XML を出力しました。")
```

---

## 設計上の判断

| 判断事項 | 選択 | 理由 |
|---|---|---|
| XML ライブラリ | 標準ライブラリ `xml.etree.ElementTree` | `lxml` は pyproject.toml に含まれていないため追加依存を避けた |
| 名前空間処理 | `ET.register_namespace("", DDI_NS)` | Python 3.9+ で動作確認。プロジェクトは `^3.11` 必須のため問題なし |
| インデント | `ET.indent(root, space="  ")` | Python 3.9+ 標準。人間が読める整形済み XML を生成 |
| 欠損フィールド | 要素を出力しない（スキップ） | DDI 2.5 の多くのフィールドは省略可能（OPTIONAL）なため、存在するフィールドのみ出力 |
| XSD バリデーション | `validate_structure()` で軽量な構造検証のみ | XSD 取得にはネットワーク接続が必要。オフライン環境でも使用できるよう独自チェックを実装 |
| DDI バージョン | `ddi_version="2.5"` をデフォルト・変更可能 | `DDIXMLSerializer(ddi_version="2.1")` で旧バージョン対応 |
