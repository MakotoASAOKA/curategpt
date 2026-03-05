# C4 統制語彙自動マッピング 実装サマリー

本ドキュメントは、DDI メタデータ生成パイプラインに追加した
**統制語彙（Controlled Vocabulary）自動マッピング（C4）** の実装内容を記録したものです。

---

## 1. 問題の概要

`PaperToDDIAgent` が LLM を使って抽出した語彙フィールドは自然言語のまま返される。

```yaml
# LLM 抽出後（改修前）
collection_mode: "郵送調査"
time_method: "横断的調査"
analysis_unit: "個人"
sampling_procedure: "層化二段無作為抽出"
```

DDI Alliance は各フィールドに対して国際標準の **Controlled Vocabulary (CV)** を定義しており、
データアーカイブ間の相互運用性のためには CV コードの使用が求められる。

```xml
<!-- DDI-Codebook 2.5 が期待する形式 -->
<collMode codeListID="ModeOfCollection" codeListVersionID="3.0">
  SelfAdministeredQuestionnaire.Paper
</collMode>
```

| フィールド | LLM 出力例 | 期待される CV コード |
|---|---|---|
| `collection_mode` | 郵送調査、留置調査 | `SelfAdministeredQuestionnaire.Paper` |
| `collection_mode` | CAPI 調査 | `Interview.FaceToFace.CAPI` |
| `time_method` | 横断調査、一時点調査 | `CrossSection` |
| `analysis_unit` | 個人、世帯 | `Individual` / `HouseholdUnit` |
| `sampling_procedure` | 層化抽出、悉皆 | `Probability.Stratified` / `TotalUniverse` |

---

## 2. 実装方針

正規表現パターン辞書による**決定論的マッピング**を採用した。

| 方式 | 採用理由 |
|---|---|
| **正規表現パターン辞書** | 追加 LLM コール不要。決定論的で監査可能。パターンの追加・修正が容易 |
| LLM に CV リストを渡す方式（不採用） | トークン消費が増加する。LLM が存在しないコードを生成するリスクがある |

マッピングは**抽出後ポストプロセス**として実行し、元の自然言語値は保持したまま
`{field}_cv` / `{field}_cv_list_id` / `{field}_cv_list_ver` の 3 並列キーを追加する。

---

## 3. 変更・追加ファイル一覧

| ファイル | 種別 | 内容 |
|---|---|---|
| `src/curategpt/wrappers/social/ddi_cv_mapper.py` | **新規** | CV マッピングモジュール本体 |
| `src/curategpt/agents/paper_to_ddi_agent.py` | 改修 | `apply_cv_mappings()` 呼び出し追加、`enable_cv_mapping` フィールド追加 |
| `src/curategpt/wrappers/social/ddi_xml_serializer.py` | 改修 | `_cv` フィールドを XML 属性に反映 |
| `DDI_PIPELINE_COMPLETE.md` | 更新 | C4 を ✅ 実装済みに更新 |

---

## 4. `ddi_cv_mapper.py` 実装詳細

### 4.1 対応フィールドと CV バージョン

| dict キー | DDI CV 名称 | バージョン | `codeListID` | パターン数 |
|---|---|---|---|---|
| `collection_mode` | ModeOfCollection | 3.0 | `ModeOfCollection` | 15 |
| `time_method` | TimeMethod | 1.2 | `TimeMethod` | 6 |
| `analysis_unit` | AnalysisUnit | 1.1 | `AnalysisUnit` | 11 |
| `sampling_procedure` | SamplingProcedure | 1.0 | なし（自由記述フィールド） | 13 |

> `sampling_procedure` は DDI-Codebook 2.5 では `<sampProc>` が自由記述フィールドであるため
> `codeListID` 属性は付与しないが、CV コードをテキストとして出力して相互運用性を高める。

### 4.2 パターン照合ルール

```python
# 正規化処理
normalized = re.sub(r"\s+", " ", value.strip())

# パターン照合（先頭から順に試行、最初のマッチが優先）
for pattern, code in patterns:
    if re.search(pattern, normalized, re.IGNORECASE):
        return code
```

- **大文字小文字を無視**（`re.IGNORECASE`）
- **空白を正規化**（連続空白を 1 スペースに統一）
- **部分一致**（`re.search`）— 接頭辞・接尾辞が付いていても一致する
- **先着優先** — 具体的なサブタイプ（`CAPI`）を汎用タイプ（`FaceToFace`）より前に置く

### 4.3 各フィールドの CV コード一覧

#### `collection_mode` — ModeOfCollection CV 3.0

| CV コード | 対応する日本語表現 | 対応する英語表現 |
|---|---|---|
| `Interview.FaceToFace.CAPI` | CAPI 調査 | CAPI, computer-assisted personal |
| `Interview.Telephone.CATI` | CATI 調査 | CATI, computer-assisted telephone |
| `SelfAdministeredQuestionnaire.CAWI` | オンライン調査、ウェブ調査 | CAWI, online, web survey |
| `SelfAdministeredQuestionnaire.CASI` | CASI 調査 | CASI, computer-assisted self |
| `Interview.FaceToFace` | 対面、訪問面接、個人面接 | face-to-face, interviewer-administered |
| `Interview.Telephone` | 電話調査、電話面接 | telephone, phone interview |
| `SelfAdministeredQuestionnaire.Paper` | 郵送調査、留置調査、配票調査 | mail questionnaire, postal survey |
| `Interview.SelfAdministered` | 自記式、自計式 | self-administered |
| `Interview.Email` | メール調査、電子メール | email survey |
| `FocusGroup` | グループインタビュー、フォーカスグループ | focus group |
| `Observation` | 観察調査 | observation |
| `RecordsAbstraction` | 行政記録、記録抽出 | records abstraction, administrative record |
| `Other` | 複合モード、混合モード | mixed mode |

#### `time_method` — TimeMethod CV 1.2

| CV コード | 対応する日本語表現 | 対応する英語表現 |
|---|---|---|
| `Longitudinal.Panel` | パネル調査、パネル | panel study, panel survey |
| `Longitudinal.Cohort` | コホート研究、コホート | cohort |
| `Longitudinal.Trend` | トレンド調査、繰り返し横断 | trend study, repeated cross-section |
| `Longitudinal` | 縦断調査、縦断 | longitudinal |
| `TimeSeries` | 時系列データ、時系列 | time series |
| `CrossSection` | 横断調査、横断、一時点、単発 | cross-section, one-time |

#### `analysis_unit` — AnalysisUnit CV 1.1

| CV コード | 対応する日本語表現 | 対応する英語表現 |
|---|---|---|
| `HouseholdUnit` | 世帯、家計 | household, hhld |
| `Family.FamilyGroup` | 家族グループ、家族世帯 | family group |
| `Family` | 家族、家庭 | family |
| `Individual` | 個人、個人単位 | individual |
| `Organization` | 企業、組織、事業所、機関、法人 | organization, enterprise, company |
| `GeographicUnit` | 地域、地区、市区町村、都道府県 | geographic, prefecture, municipality |
| `Group` | 集団、グループ | group |
| `TextUnit` | テキスト単位、文書単位 | text unit |
| `Event` | イベント、出来事、事象 | event |
| `Object` | 物品、モノ、製品 | object |
| `TimeUnit` | 時間単位、期間 | time unit |

#### `sampling_procedure` — SamplingProcedure CV 1.0（上位カテゴリのみ）

| CV コード | 対応する日本語表現 | 対応する英語表現 |
|---|---|---|
| `TotalUniverse` | 悉皆、全数調査、全件調査 | census, complete enumeration |
| `Probability.SimpleRandom` | 単純無作為抽出、単純ランダム | simple random |
| `Probability.Stratified` | 層化抽出、層別抽出 | stratified |
| `Probability.Cluster` | クラスター抽出、多段抽出 | cluster, multi-stage |
| `Probability.Systematic` | 系統抽出、等間隔抽出 | systematic |
| `Probability` | 確率抽出、確率的標本 | probability sampling |
| `NonProbability.Quota` | 割当抽出、クォータ | quota |
| `NonProbability.Purposive` | 有意抽出、目的的 | purposive |
| `NonProbability.Snowball` | スノーボール | snowball |
| `NonProbability.Convenience` | 便宜的抽出 | convenience |
| `NonProbability.VolunteerSample` | 自発的、自由参加 | voluntary |
| `NonProbability` | 非確率抽出 | non-probability |
| `Mixed` | 混合抽出、組み合わせ | mixed method |

### 4.4 公開 API

```python
from curategpt.wrappers.social.ddi_cv_mapper import map_to_cv, apply_cv_mappings

# 単一フィールドのマッピング
code = map_to_cv("collection_mode", "郵送調査")
# → "SelfAdministeredQuestionnaire.Paper"

code = map_to_cv("collection_mode", "unknown")
# → None  (パターン不一致)

# DDI dict への一括適用（推奨）
ddi = apply_cv_mappings(ddi_dict)
```

### 4.5 出力キーの構造

```python
# 入力
ddi = {
    "collection_mode": "郵送調査",
    "time_method":     "横断調査",
    "analysis_unit":   "世帯",
    "sampling_procedure": "層化二段無作為抽出",
}

# apply_cv_mappings() 適用後（元値は保持）
{
    "collection_mode":              "郵送調査",                          # 元値（変更なし）
    "collection_mode_cv":           "SelfAdministeredQuestionnaire.Paper", # CV コード
    "collection_mode_cv_list_id":   "ModeOfCollection",                  # codeListID
    "collection_mode_cv_list_ver":  "3.0",                               # codeListVersionID

    "time_method":                  "横断調査",
    "time_method_cv":               "CrossSection",
    "time_method_cv_list_id":       "TimeMethod",
    "time_method_cv_list_ver":      "1.2",

    "analysis_unit":                "世帯",
    "analysis_unit_cv":             "HouseholdUnit",
    "analysis_unit_cv_list_id":     "AnalysisUnit",
    "analysis_unit_cv_list_ver":    "1.1",

    "sampling_procedure":           "層化二段無作為抽出",
    "sampling_procedure_cv":        "Probability.Stratified",
    "sampling_procedure_cv_list_id": None,   # sampProc は自由記述フィールド
    "sampling_procedure_cv_list_ver": None,
}
```

---

## 5. `paper_to_ddi_agent.py` への統合

### 追加インポート

```python
from curategpt.wrappers.social.ddi_cv_mapper import apply_cv_mappings
```

### 新規 dataclass フィールド

```python
@dataclass
class PaperToDDIAgent(BaseAgent):
    context_tokens: int = _DEFAULT_CONTEXT_TOKENS
    rag_example_limit: int = 3
    enable_source_check: bool = True
    enable_cv_mapping: bool = True          # ← 追加
```

### 呼び出し順序（`extract_ddi_from_text()` 内）

```python
merged = self._merge_extractions(group_results)

# C1: Hallucination 原文照合チェック
if self.enable_source_check:
    merged = self._check_against_source(merged, text)

# C4: 統制語彙自動マッピング（Hallucination チェックの後に実行）
if self.enable_cv_mapping:
    merged = apply_cv_mappings(merged)

# タイトルフォールバック
if not merged.get("title") and filename:
    merged["title"] = Path(filename).stem
```

> **実行順序の理由**: `enable_source_check` で `collection_mode` 等が null 化された後に
> CV マッピングを実行しても空振りするだけなので問題ない。
> Hallucination チェックが先に不正値を除去することで、CV マッピングが誤った値に
> コードを割り当てるリスクを排除できる。

---

## 6. `ddi_xml_serializer.py` への統合

### `_build_method()` の変更

```python
# 改修前: 単純なテキスト出力
for xml_tag, text in entries:
    _sub(data_coll, xml_tag, text=text)

# 改修後: _cv フィールドがあれば codeListID/VersionID 属性を付加
for field_key, xml_tag in method_fields:
    raw = _str_or_none(d.get(field_key))
    if not raw:
        continue
    cv_code    = d.get(f"{field_key}_cv")
    cv_list_id = d.get(f"{field_key}_cv_list_id")
    cv_list_ver = d.get(f"{field_key}_cv_list_ver")

    if cv_code and cv_list_id:
        # CV コードを要素テキストに、属性を追加
        attribs = {"codeListID": cv_list_id, "codeListVersionID": cv_list_ver}
        _sub(data_coll, xml_tag, text=cv_code, **attribs)
    elif cv_code:
        # CV コードはあるが codeListID なし（sampProc）
        _sub(data_coll, xml_tag, text=cv_code)
    else:
        # マッピング不一致：元の自然言語値をそのまま出力
        _sub(data_coll, xml_tag, text=raw)
```

### `_build_sum_dscr()` の変更（`anlyUnit` 対応）

```python
# 改修前
if analysis_unit:
    _sub(sum_dscr, "anlyUnit", text=analysis_unit)

# 改修後
if analysis_unit:
    cv_code = d.get("analysis_unit_cv")
    cv_list_id = d.get("analysis_unit_cv_list_id")
    cv_list_ver = d.get("analysis_unit_cv_list_ver")
    if cv_code and cv_list_id:
        _sub(sum_dscr, "anlyUnit", text=cv_code,
             codeListID=cv_list_id, codeListVersionID=cv_list_ver)
    else:
        _sub(sum_dscr, "anlyUnit", text=analysis_unit)
```

### XML 出力の変化（入出力比較）

```xml
<!-- 改修前: 自然言語そのまま -->
<collMode>郵送調査</collMode>
<timeMeth>横断調査</timeMeth>
<anlyUnit>世帯</anlyUnit>
<sampProc>層化二段無作為抽出</sampProc>

<!-- 改修後: CV コード + codeListID 属性 -->
<collMode codeListID="ModeOfCollection" codeListVersionID="3.0">
  SelfAdministeredQuestionnaire.Paper
</collMode>
<timeMeth codeListID="TimeMethod" codeListVersionID="1.2">
  CrossSection
</timeMeth>
<anlyUnit codeListID="AnalysisUnit" codeListVersionID="1.1">
  HouseholdUnit
</anlyUnit>
<sampProc>Probability.Stratified</sampProc>
```

---

## 7. テスト結果

### `map_to_cv()` 単体テスト（24 ケース）

| カテゴリ | テスト数 | 結果 |
|---|---|---|
| `collection_mode`（日本語・英語・略称） | 9 | ✅ 全パス |
| `time_method`（日本語・英語） | 5 | ✅ 全パス |
| `analysis_unit`（日本語・英語） | 4 | ✅ 全パス |
| `sampling_procedure`（日本語・英語） | 5 | ✅ 全パス |
| パターン不一致（None 返却） | 1 | ✅ パス |

### `apply_cv_mappings()` 統合テスト

```
collection_mode_cv           = 'SelfAdministeredQuestionnaire.Paper'  ✅
collection_mode_cv_list_id   = 'ModeOfCollection'                      ✅
collection_mode_cv_list_ver  = '3.0'                                   ✅
time_method_cv               = 'CrossSection'                           ✅
analysis_unit_cv             = 'Individual'                             ✅
sampling_procedure_cv        = 'Probability.Stratified'                 ✅
sampling_procedure_cv_list_id = None  (sampProc は自由記述フィールド)   ✅
```

### XML 属性出力テスト（7 ケース）

| XML 要素 | 属性 | 期待値 | 結果 |
|---|---|---|---|
| `<collMode>` | `codeListID` | `ModeOfCollection` | ✅ |
| `<collMode>` | `codeListVersionID` | `3.0` | ✅ |
| `<timeMeth>` | `codeListID` | `TimeMethod` | ✅ |
| `<timeMeth>` | `codeListVersionID` | `1.2` | ✅ |
| `<anlyUnit>` | `codeListID` | `AnalysisUnit` | ✅ |
| `<anlyUnit>` | `codeListVersionID` | `1.1` | ✅ |
| `<sampProc>` | `codeListID` | なし（`None`） | ✅ |

---

## 8. 使用方法

### パイプライン全体（推奨）

```python
from curategpt.agents.paper_to_ddi_agent import PaperToDDIAgent

agent = PaperToDDIAgent(
    knowledge_source=db,
    extractor=extractor,
    enable_source_check=True,   # Hallucination 原文照合
    enable_cv_mapping=True,     # 統制語彙マッピング（デフォルト有効）
)

ddi = agent.extract_ddi_from_file("survey_report.pdf")

# 抽出後のフィールド確認
print(ddi["collection_mode"])        # 郵送調査（元の自然言語）
print(ddi["collection_mode_cv"])     # SelfAdministeredQuestionnaire.Paper
```

### マッパー単体での利用

```python
from curategpt.wrappers.social.ddi_cv_mapper import map_to_cv, apply_cv_mappings

# 単一値のマッピング
print(map_to_cv("collection_mode", "郵送調査"))
# → "SelfAdministeredQuestionnaire.Paper"

print(map_to_cv("time_method", "panel study"))
# → "Longitudinal.Panel"

print(map_to_cv("collection_mode", "不明な調査方式"))
# → None  (マッピング不一致 → 元値をそのまま保持)

# dict への一括適用
ddi = {"collection_mode": "CAPI 調査", "time_method": "コホート研究"}
apply_cv_mappings(ddi)
print(ddi["collection_mode_cv"])   # Interview.FaceToFace.CAPI
print(ddi["time_method_cv"])       # Longitudinal.Cohort
```

### CV マッピングを無効化する場合

```python
# 表記が CV パターンと大きく異なる文書など
agent = PaperToDDIAgent(
    knowledge_source=db,
    extractor=extractor,
    enable_cv_mapping=False,    # 無効化 → 元の自然言語値のみ dict に残る
)
```

---

## 9. 設計上の判断事項

| 判断事項 | 採用した設計 | 理由 |
|---|---|---|
| 元値の保持 | 元の自然言語値を残し、`_cv` キーを並列追加 | 人手確認時に元の文脈が分かる。`_cv` キーが使えない場合でも後方互換 |
| `sampling_procedure` の属性 | `codeListID` なし、CV コードはテキストとして出力 | DDI-Codebook 2.5 の `<sampProc>` はスキーマ上で自由記述フィールド |
| CV マッピング不一致時 | `{field}_cv = None`、元値で XML 出力 | マッピング失敗で XML が欠落するより、不完全でも元値を出力する方が実用的 |
| 実行タイミング | Hallucination チェック（L3）の後 | 不正値を除去した後にマッピングすることで誤コード割り当てを防ぐ |
| パターンの優先順位 | より具体的なコード（`CAPI`）を汎用（`FaceToFace`）より先に配置 | `re.search` は部分一致なので「CAPI」が「FaceToFace」のパターンにも誤マッチするため |

---

## 10. 今後の拡張候補

| # | 内容 | 工数 |
|---|---|---|
| M1 | `topic_classification` の DDI Alliance Subject Classification CV へのマッピング | 中 |
| M2 | パターン辞書を外部 YAML/JSON ファイルとして管理可能にする（設定ファイル化） | 小 |
| M3 | マッチしなかった値のログ収集と集計レポート出力（カバレッジ改善支援） | 小 |
| M4 | LLM フォールバック：パターン不一致時に CV コード一覧を LLM に渡して選択させる | 中 |
