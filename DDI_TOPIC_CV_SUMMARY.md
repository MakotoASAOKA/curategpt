# DDI TopicClassification CV 対応まとめ

## 概要

CESSDA TopicClassification 4.2.2 による `topic_classification` フィールドの統制語彙自動マッピングを実装した。
LLM が返す自由記述のトピック文字列を CESSDA CV コードに変換し、DDI-Codebook 2.5 XML の `<topcClas>` 要素に
`codeListID` / `codeListVersionID` 属性を付与して出力する。

- **CV 名称**: TopicClassification
- **バージョン**: 4.2.2
- **コード数**: 82（トップレベル 19 + サブコード 63）
- **取得元 API**: `https://vocabularies.cessda.eu/v2/codes/TopicClassification/4.2.2/en`

---

## 変更ファイル一覧

| ファイル | 変更内容 |
|---|---|
| `src/curategpt/wrappers/social/ddi_cv_mapper.py` | `_TOPIC_PATTERNS` 追加、レジストリ更新、`apply_cv_mappings()` リスト対応 |
| `src/curategpt/wrappers/social/ddi_xml_serializer.py` | `_build_stdy_info()` を CV コード出力対応に更新 |

---

## 実装詳細

### 1. `_TOPIC_PATTERNS` の追加 (`ddi_cv_mapper.py`)

82 コード全てに対して英語・日本語の正規表現パターンを定義した。
パターンの適用順序は **サブコード優先（specificity-first）** とし、より具体的なコードが先にマッチするよう設計した。

```python
_TOPIC_PATTERNS: List[Tuple[str, str]] = [
    # Demography sub-codes (先に記述)
    (r"census|センサス|国勢調査|人口調査",           "Demography.Censuses"),
    (r"migration|immigration|移住|移民|人口移動",    "Demography.Migration"),
    (r"morbidity|mortality|死亡率|罹患率",           "Demography.MorbidityAndMortality"),
    # Demography top-level (後に記述)
    (r"demography|demographic|population|人口統計|人口", "Demography"),
    # ... (全82コード)
]
```

#### 対応コード一覧（トップレベル19カテゴリ）

| トップレベルコード | サブコード数 | 日本語パターン例 |
|---|---|---|
| `Demography` | 3 | 人口、国勢調査、移民 |
| `Economics` | 5 | 経済、所得、財政、消費 |
| `Education` | 5 | 教育、高等教育、職業訓練 |
| `Health` | 11 | 健康、公衆衛生、疾患、治療 |
| `History` | 0 | 歴史 |
| `HousingAndLandUse` | 2 | 住宅、土地利用 |
| `LabourAndEmployment` | 7 | 労働、雇用、失業、賃金 |
| `LawCrimeAndLegalSystems` | 2 | 法律、犯罪、司法 |
| `MediaCommunicationAndLanguage` | 4 | メディア、言語、情報社会 |
| `NaturalEnvironment` | 4 | 環境、エネルギー、生物 |
| `Politics` | 6 | 政治、選挙、安全保障 |
| `Psychology` | 0 | 心理学、メンタルヘルス |
| `ScienceAndTechnology` | 2 | 科学技術、IT |
| `SocialStratificationAndGroupings` | 9 | 階層、高齢者、ジェンダー、子ども |
| `SocialWelfarePolicyAndSystems` | 3 | 福祉、社会保障、介護 |
| `SocietyAndCulture` | 9 | 文化、余暇、生活時間、宗教 |
| `TradeIndustryAndMarkets` | 3 | 産業、農業、貿易 |
| `TransportAndTravel` | 0 | 交通、旅行 |
| `Other` | 0 | その他 |

---

### 2. `_FIELD_REGISTRY` への追加 (`ddi_cv_mapper.py`)

`topic_classification` はリスト型フィールドであるため、`is_list: True` フラグを新設した。
（他の4フィールドには `is_list: False` を追加）

```python
_FIELD_REGISTRY: Dict[str, Dict[str, Any]] = {
    "collection_mode":     { ..., "is_list": False },
    "time_method":         { ..., "is_list": False },
    "analysis_unit":       { ..., "is_list": False },
    "sampling_procedure":  { ..., "is_list": False },
    "topic_classification": {
        "patterns":       _TOPIC_PATTERNS,
        "code_list_id":   "TopicClassification",
        "code_list_ver":  "4.2.2",
        "is_list":        True,          # ← 新設フラグ
    },
}
```

---

### 3. `apply_cv_mappings()` のリストフィールド対応 (`ddi_cv_mapper.py`)

`is_list: True` のフィールドについて、リスト各要素を個別にマッピングし、
結果を **コードのリスト** として `{field}_cv` キーに格納するよう変更した。

```python
if is_list:
    items = val if isinstance(val, list) else [val]
    codes = [map_to_cv(field, item) for item in items]  # 各要素を個別マッピング
    ddi_dict[f"{field}_cv"] = codes          # List[Optional[str]]
    ddi_dict[f"{field}_cv_list_id"] = meta["code_list_id"]
    ddi_dict[f"{field}_cv_list_ver"] = meta["code_list_ver"]
```

#### 変換後のキー構造

```python
ddi = apply_cv_mappings({
    "topic_classification": ["Public health", "Elections", "教育", "UNKNOWN"]
})

ddi["topic_classification"]             # ["Public health", "Elections", "教育", "UNKNOWN"]  (原文保持)
ddi["topic_classification_cv"]          # ["Health.PublicHealth", "Politics.Elections", "Education", None]
ddi["topic_classification_cv_list_id"]  # "TopicClassification"
ddi["topic_classification_cv_list_ver"] # "4.2.2"
```

---

### 4. `ddi_xml_serializer.py` の `topcClas` 出力更新

`_build_stdy_info()` 内の `<topcClas>` 生成ロジックを以下のルールで更新した：

- CV コードが存在する場合 → CV コードをテキストとし、`codeListID` / `codeListVersionID` 属性を付与
- CV コードが `None`（マッチなし）の場合 → 原文テキストをフォールバック、属性なし

```python
for i, tc in enumerate(topics):
    cv_code = topic_cv_codes[i] if i < len(topic_cv_codes) else None
    if cv_code and tc_list_id:
        tc_attrs = {"codeListID": tc_list_id, "codeListVersionID": tc_list_ver}
        _sub(subject, "topcClas", text=cv_code, **tc_attrs)
    else:
        _sub(subject, "topcClas", text=tc)   # フォールバック
```

---

## 出力 XML 例

```xml
<!-- CV コードあり（3件マッチ） -->
<topcClas codeListID="TopicClassification" codeListVersionID="4.2.2">Health.PublicHealth</topcClas>
<topcClas codeListID="TopicClassification" codeListVersionID="4.2.2">Politics.Elections</topcClas>
<topcClas codeListID="TopicClassification" codeListVersionID="4.2.2">Education</topcClas>

<!-- マッチなし → 原文フォールバック -->
<topcClas>UNKNOWN_TOPIC</topcClas>
```

---

## テスト結果

### `map_to_cv("topic_classification", ...)` — 25ケース

| 入力値 | 期待コード | 結果 |
|---|---|---|
| `"Health"` | `Health` | ✅ |
| `"Public health"` | `Health.PublicHealth` | ✅ |
| `"Diet and nutrition"` | `Health.DietAndNutrition` | ✅ |
| `"Elections"` | `Politics.Elections` | ✅ |
| `"Political behaviour and attitudes"` | `Politics.PoliticalBehaviourAndAttitudes` | ✅ |
| `"Family life and marriage"` | `SocialStratificationAndGroupings.FamilyLifeAndMarriage` | ✅ |
| `"Gender and gender roles"` | `SocialStratificationAndGroupings.GenderAndGenderRoles` | ✅ |
| `"Children"` | `SocialStratificationAndGroupings.Children` | ✅ |
| `"Elderly"` | `SocialStratificationAndGroupings.Elderly` | ✅ |
| `"Education"` | `Education` | ✅ |
| `"Higher and further education"` | `Education.HigherAndFurtherEducation` | ✅ |
| `"Social welfare"` | `SocialWelfarePolicyAndSystems` | ✅ |
| `"Retirement"` | `LabourAndEmployment.Retirement` | ✅ |
| `"Unemployment"` | `LabourAndEmployment.Unemployment` | ✅ |
| `"Working conditions"` | `LabourAndEmployment.WorkingConditions` | ✅ |
| `"Time use"` | `SocietyAndCulture.TimeUse` | ✅ |
| `"Community, urban and rural life"` | `SocietyAndCulture.CommunityUrbanAndRuralLife` | ✅ |
| `"公衆衛生"` | `Health.PublicHealth` | ✅ |
| `"選挙"` | `Politics.Elections` | ✅ |
| `"高齢者"` | `SocialStratificationAndGroupings.Elderly` | ✅ |
| `"ジェンダー"` | `SocialStratificationAndGroupings.GenderAndGenderRoles` | ✅ |
| `"労働条件"` | `LabourAndEmployment.WorkingConditions` | ✅ |
| `"生活時間"` | `SocietyAndCulture.TimeUse` | ✅ |
| `"教育"` | `Education` | ✅ |
| `"人口"` | `Demography` | ✅ |

**25/25 全件パス**

### リストフィールド処理テスト

```python
input:  ["Public health", "Elections", "教育", "UNKNOWN_TOPIC"]
output: ["Health.PublicHealth", "Politics.Elections", "Education", None]
```
✅ マッチなし要素は `None`、原文はフォールバックで XML 出力

### XML 出力テスト

| 入力 | XML テキスト | codeListID | codeListVersionID |
|---|---|---|---|
| `"Public health"` | `Health.PublicHealth` | `TopicClassification` | `4.2.2` |
| `"Elections"` | `Politics.Elections` | `TopicClassification` | `4.2.2` |
| `"教育"` | `Education` | `TopicClassification` | `4.2.2` |
| `"UNKNOWN_TOPIC"` | `UNKNOWN_TOPIC` | （なし） | （なし） |

✅ 全件正常出力

---

## 設計上の判断

### スカラー vs リストの区別

`topic_classification` は一件の調査に複数トピックが割り当てられる（例：「健康」「高齢者」「家族」）ため、
`is_list: True` フラグを導入してリスト全体を処理する設計とした。
他の4フィールド（`collection_mode` 等）は単一値であり従来通り。

### サブコード優先の順序

`_TOPIC_PATTERNS` において、サブコード（例 `Health.PublicHealth`）を必ずトップレベル（`Health`）より前に配置した。
これにより「Public health」と入力された場合に `Health` ではなく `Health.PublicHealth` が返される。

### フォールバック戦略

マッチしない場合は `None` を返し、XML シリアライザがフォールバックとして原文を出力する。
これにより CV コードへの変換失敗が出力の欠損につながらない。

### 日本語パターンの範囲

主要カテゴリ（健康、政治、教育、労働、社会など）に絞って日本語パターンを追加した。
日本語の社会調査で頻出するトピックをカバーしており、日本語の論文・報告書からの抽出に対応する。

---

## 使用方法

```python
from curategpt.agents.paper_to_ddi_agent import PaperToDDIAgent
from curategpt.wrappers.social.ddi_xml_serializer import to_ddi_xml

agent = PaperToDDIAgent(extractor=extractor, enable_cv_mapping=True)
ddi = agent.extract_ddi_from_file("survey_report.pdf")

# ddi["topic_classification"]    → ["Public health", "Elderly"]  (原文)
# ddi["topic_classification_cv"] → ["Health.PublicHealth", "SocialStratificationAndGroupings.Elderly"]

xml_bytes = to_ddi_xml(ddi)
```

---

## 関連ドキュメント

| ファイル | 内容 |
|---|---|
| `DDI_CV_MAPPER_SUMMARY.md` | C4 統制語彙マッピング全般（ModeOfCollection 等）の概要 |
| `DDI_PIPELINE_COMPLETE.md` | DDI 生成パイプライン全体の手順と課題 |
| `DDI_HALLUCINATION_GUARD_SUMMARY.md` | C1 ハルシネーション対策の実装詳細 |
