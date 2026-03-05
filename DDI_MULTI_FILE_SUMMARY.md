# DDI 複数ファイル一括処理（D1）実装まとめ

## 概要

`PaperToDDIAgent` に `extract_ddi_from_files()` メソッドを追加し、
調査報告書・調査票などの複数ファイルをロール（役割）に基づいて一括処理し、
DDI メタデータを自動マージして返す機能（D1）を実装した。

従来は報告書と調査票を個別に処理して手動マージする必要があったが、
本実装によりワンコールで完結する。

---

## 変更ファイル一覧

| ファイル | 変更内容 |
|---|---|
| `src/curategpt/agents/paper_to_ddi_agent.py` | `_ROLE_GROUPS` 定数・`_resolve_role()` 関数・`_run_extraction_groups()` ヘルパー・`extract_ddi_from_files()` を追加。`extract_ddi_from_text()` を内部リファクタ |
| `DDI_PIPELINE_COMPLETE.md` | D1 を ✅ 実装済みに更新。Step 3 コード例・API早見表を新APIに更新 |

---

## 実装詳細

### 1. `_ROLE_GROUPS` 定数と `_resolve_role()` 関数

ロール名と実行する抽出グループの対応を定数として定義した。

```python
_ROLE_GROUPS: Dict[str, List[str]] = {
    "report":        ["bibliographic", "methodology"],
    "questionnaire": ["survey"],
    "all":           ["bibliographic", "methodology", "survey"],
}
```

`_resolve_role()` はロール名の**前方一致**でグループを解決するため、
`"report_2023"` → `"report"`、`"questionnaire_wave2"` → `"questionnaire"` のように
年次や波番号付きのロール名にも対応する。

```python
def _resolve_role(role: str) -> List[str]:
    if role in _ROLE_GROUPS:
        return _ROLE_GROUPS[role]
    for key in _ROLE_GROUPS:
        if role.startswith(key):
            return _ROLE_GROUPS[key]
    # 未知ロールは全グループを実行（安全なデフォルト）
    return list(EXTRACTION_GROUPS.keys())
```

| ロール名例 | 解決されるグループ |
|---|---|
| `"report"` | bibliographic, methodology |
| `"report_2023"` | bibliographic, methodology |
| `"questionnaire"` | survey |
| `"questionnaire_wave2"` | survey |
| `"all"` | bibliographic, methodology, survey |
| `"unknown"` | bibliographic, methodology, survey（警告付き） |

---

### 2. `_run_extraction_groups()` ヘルパー（内部メソッド）

`extract_ddi_from_text()` の内部ロジックを独立したヘルパーに切り出した。
節検出・グループ別 LLM 呼び出し・Hallucination source-check を担い、
単一ファイル処理（`extract_ddi_from_text`）と複数ファイル処理（`extract_ddi_from_files`）
の両方から呼び出される共通処理となっている。

```python
def _run_extraction_groups(
    self,
    text: str,
    filename: str = "",
    group_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    指定グループのみ抽出し、そのファイルの原文に対して source-check を適用する。
    CV マッピングは呼び出し元で実施（複数ファイルのマージ後に一括適用するため）。
    """
```

**CV マッピングをヘルパーに含めない理由：**
複数ファイルの結果をマージした後に一括で `apply_cv_mappings()` を呼ぶ設計とした。
ファイルごとに CV マッピングすると、リスト型フィールド（`topic_classification` 等）が
各ファイル分重複して処理される可能性があるため。

---

### 3. `extract_ddi_from_files()` 新規メソッド（公開 API）

```python
def extract_ddi_from_files(
    self,
    files: Union[Dict[str, str], List[Tuple[str, str]]],
) -> Dict[str, Any]:
```

#### 引数形式

**dict 形式**（最も一般的）：

```python
ddi = agent.extract_ddi_from_files({
    "report":        "survey_report.pdf",   # 書誌 + 方法論を抽出
    "questionnaire": "questionnaire.pdf",   # 変数情報を抽出
})
```

**リスト形式**（同一ロールの複数ファイルに対応）：

```python
ddi = agent.extract_ddi_from_files([
    ("report",        "report_wave1.pdf"),
    ("report",        "report_wave2.pdf"),
    ("questionnaire", "questionnaire.pdf"),
])
```

dict は内部でリストに正規化されるため、処理ロジックは統一されている。

#### 処理フロー

```
files (dict or list)
    │
    │ 正規化 → List[Tuple[role, path]]
    │
    ├─── role="report" ─────────────────────────────────────────────┐
    │    text = _read_full_document("survey_report.pdf")            │
    │    partial_1 = _run_extraction_groups(                        │
    │        text, groups=["bibliographic","methodology"]           │
    │    )  ← source-check はこのファイルの原文に対して実施          │
    │                                                               │
    ├─── role="questionnaire" ──────────────────────────────────────┤
    │    text = _read_full_document("questionnaire.pdf")            │
    │    partial_2 = _run_extraction_groups(                        │
    │        text, groups=["survey"]                                │
    │    )  ← source-check はこのファイルの原文に対して実施          │
    │                                                               │
    ▼                                                               │
_merge_extractions([partial_1, partial_2])                         │
    ← スカラーフィールド: ファイルの順序で先着優先                   │
    ← リストフィールド: 重複排除してマージ                           │
    │                                                               │
apply_cv_mappings(merged)  ← CV マッピングは全マージ後に一括適用    │
    │                                                               │
title フォールバック                                                 │
    ← title が未取得の場合、最初の non-questionnaire ファイル名を使用│
    │                                                               │
DDI metadata dict ◄─────────────────────────────────────────────────┘
```

#### マージ戦略

| フィールド種別 | マージ方針 | 備考 |
|---|---|---|
| スカラー（`title`, `universe` 等） | 先着優先（最初に取得した値を保持） | report を先に渡すと report の値が優先される |
| リスト（`variables`, `keywords` 等） | 重複排除マージ（repr で一意性判定） | 両ファイルに変数があれば結合される |
| `None` 値 | スキップ（後続ファイルの値を採用） | 片方が null でも他方の値が有効 |

---

### 4. `extract_ddi_from_text()` のリファクタ

既存の公開 API は変更なし。内部処理を `_run_extraction_groups()` の呼び出しに置き換えた。

```python
# 変更前
def extract_ddi_from_text(self, text, filename=""):
    sections = self._extract_sections(text)
    for group_name, group_cfg in EXTRACTION_GROUPS.items():
        ...  # 3グループ分のループ
    merged = self._merge_extractions(group_results)
    if self.enable_source_check:
        merged = self._check_against_source(merged, text)
    if self.enable_cv_mapping:
        merged = apply_cv_mappings(merged)
    ...

# 変更後（後方互換を保ちつつ内部整理）
def extract_ddi_from_text(self, text, filename=""):
    merged = self._run_extraction_groups(          # ← 共通ヘルパーに委譲
        text=text,
        filename=filename,
        group_names=list(EXTRACTION_GROUPS.keys()),
    )
    if self.enable_cv_mapping:
        merged = apply_cv_mappings(merged)
    if not merged.get("title") and filename:
        merged["title"] = Path(filename).stem
    return merged
```

---

## テスト結果

| テスト | 結果 |
|---|---|
| `_resolve_role("report")` → `["bibliographic","methodology"]` | ✅ |
| `_resolve_role("report_2023")` → `["bibliographic","methodology"]`（前方一致） | ✅ |
| `_resolve_role("questionnaire")` → `["survey"]` | ✅ |
| `_resolve_role("questionnaire_wave2")` → `["survey"]`（前方一致） | ✅ |
| `_resolve_role("all")` → 全3グループ | ✅ |
| `_run_extraction_groups()` モック LLM での実行 | ✅ |
| `extract_ddi_from_files()` dict→list 正規化 | ✅ |
| `extract_ddi_from_text()` が `_run_extraction_groups` を使用 | ✅ |

**全 8 テストケース PASSED**

---

## 使用例

### 標準的なケース（報告書 + 調査票）

```python
from curategpt.agents.paper_to_ddi_agent import PaperToDDIAgent

agent = PaperToDDIAgent(
    knowledge_source=db,
    extractor=extractor,
    context_tokens=128_000,
    enable_source_check=True,
    enable_cv_mapping=True,
)

ddi = agent.extract_ddi_from_files({
    "report":        "survey_report_2023.pdf",
    "questionnaire": "questionnaire_2023.pdf",
})

print(ddi["title"])                        # 報告書から抽出
print(ddi["collection_mode_cv"])           # ModeOfCollection CV コード
print(ddi["topic_classification_cv"])      # CESSDA TopicClassification コード
print(len(ddi.get("variables", [])))       # 調査票から抽出した変数数
```

### 報告書のみ（変数抽出なし）

```python
ddi = agent.extract_ddi_from_files({"report": "survey_report.pdf"})
```

### 調査票のみ（変数抽出のみ）

```python
ddi = agent.extract_ddi_from_files({"questionnaire": "questionnaire.pdf"})
```

### 複数年次調査の報告書マージ

```python
ddi = agent.extract_ddi_from_files([
    ("report",        "report_2021.pdf"),
    ("report",        "report_2022.pdf"),   # 2021 にないフィールドを補完
    ("questionnaire", "questionnaire.pdf"),
])
```

### 従来の単一ファイル処理（後方互換、変更なし）

```python
# これまで通り動作する
ddi = agent.extract_ddi_from_file("survey_report.pdf")
ddi = agent.extract_ddi_from_text(text_str, filename="report.txt")
```

---

## 設計上の判断

### source-check はファイルごとに実施

Hallucination source-check（`_check_against_source()`）は各ファイルの
抽出直後に、そのファイルの原文テキストに対して実施する。
マージ後にまとめて実施すると、ファイルAで正当な値がファイルBの原文には
含まれないために誤って null 化される可能性があるため、
ファイル単位での照合が適切と判断した。

### CV マッピングはマージ後に一括適用

`apply_cv_mappings()` はファイルごとではなくマージ後に一度だけ呼ぶ。
これにより `topic_classification_cv` 等のリスト型 CV フィールドが
複数回生成されて重複する問題を回避している。

### スカラーフィールドの先着優先

`_merge_extractions()` はスカラーフィールドを「最初の非 None 値優先」で
マージする。dict 形式で `{"report": ..., "questionnaire": ...}` と渡した場合、
Python 3.7 以降の dict は挿入順を保持するため、report の値が questionnaire
の値より優先される。使用者が意図的に優先度を制御したい場合はリスト形式で
渡す順序を調整すればよい。

### 未知ロールへの対応

定義されていないロール名が渡された場合は WARNING ログを出力したうえで
全グループを実行する（データを欠損させない安全なデフォルト）。

---

## 関連ドキュメント

| ファイル | 内容 |
|---|---|
| `DDI_PIPELINE_COMPLETE.md` | DDI 生成パイプライン全体の手順と課題（D1 ✅ に更新済み） |
| `DDI_CV_MAPPER_SUMMARY.md` | C4 統制語彙マッピングの実装詳細 |
| `DDI_TOPIC_CV_SUMMARY.md` | CESSDA TopicClassification 対応の実装詳細 |
| `DDI_HALLUCINATION_GUARD_SUMMARY.md` | C1 Hallucination 対策の実装詳細 |
