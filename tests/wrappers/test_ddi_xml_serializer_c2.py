"""Tests for C2 – filter question / branching structure support.

Covers:
- DDIXMLSerializer._build_var(): <filtrInstr> element emitted when filter_condition is set
- filter_condition=None → no <filtrInstr>, no <qstn> element (if question also absent)
- filter_condition with question present → both <qstnLit> and <filtrInstr> inside <qstn>
- filter_condition without question → <qstn> with only <filtrInstr>
- Other variable fields unaffected by filter_condition presence/absence
- paper_to_ddi_agent survey instructions contain filter_condition field description
"""

import importlib.util
import sys
import types
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

# ---------------------------------------------------------------------------
# Minimal stubs (avoid importing the full curategpt package)
# ---------------------------------------------------------------------------

def _make_stub(name, attrs=None):
    mod = types.ModuleType(name)
    if attrs:
        for k, v in attrs.items():
            setattr(mod, k, v)
    return mod


_STUB_NAMES = [
    "yaml",
    "pdfplumber",
    "textract",
    "curategpt",
    "curategpt.agents",
    "curategpt.agents.base_agent",
    "curategpt.wrappers",
    "curategpt.wrappers.social",
    "curategpt.wrappers.social.ddi_cv_mapper",
]
for _name in _STUB_NAMES:
    if _name not in sys.modules:
        sys.modules[_name] = _make_stub(_name)

yaml_stub = sys.modules["yaml"]
yaml_stub.safe_load = lambda s: {}
yaml_stub.dump = lambda obj, **kw: ""
yaml_stub.YAMLError = Exception
sys.modules["curategpt.wrappers.social.ddi_cv_mapper"].apply_cv_mappings = lambda d: d


class _BaseAgent:
    knowledge_source = None
    knowledge_source_collection = None
    extractor = None


sys.modules["curategpt.agents.base_agent"].BaseAgent = _BaseAgent

_BASE = Path(__file__).parent.parent.parent / "src" / "curategpt"

# Load ddi_xml_serializer directly
_SER_PATH = _BASE / "wrappers" / "social" / "ddi_xml_serializer.py"
_ser_spec = importlib.util.spec_from_file_location("ddi_xml_serializer", _SER_PATH)
ser_mod = importlib.util.module_from_spec(_ser_spec)
_ser_spec.loader.exec_module(ser_mod)
DDI_NS = ser_mod.DDI_NS
DDIXMLSerializer = ser_mod.DDIXMLSerializer

# Load paper_to_ddi_agent directly
_AGENT_PATH = _BASE / "agents" / "paper_to_ddi_agent.py"
_ag_spec = importlib.util.spec_from_file_location("paper_to_ddi_agent", _AGENT_PATH)
agent_mod = importlib.util.module_from_spec(_ag_spec)
_ag_spec.loader.exec_module(agent_mod)
EXTRACTION_GROUPS = agent_mod.EXTRACTION_GROUPS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NS = f"{{{DDI_NS}}}"


def _parse(xml_str: str) -> ET.Element:
    return ET.fromstring(xml_str)


def _find(root: ET.Element, path: str):
    parts = path.split("/")
    el = root
    for part in parts:
        el = el.find(f"{_NS}{part}")
        if el is None:
            return None
    return el


def _build_minimal_ddi(variables):
    return {
        "id": "C2-test",
        "title": "C2 テスト調査",
        "principal_investigators": ["テスト太郎"],
        "variables": variables,
    }


# ---------------------------------------------------------------------------
# Tests: DDIXMLSerializer._build_var() with filter_condition (C2)
# ---------------------------------------------------------------------------

class TestBuildVarFilterCondition(unittest.TestCase):
    """Tests for <filtrInstr> emission from _build_var()."""

    def _serialise(self, variables):
        s = DDIXMLSerializer()
        xml_str = s.to_xml_string(_build_minimal_ddi(variables))
        return _parse(xml_str)

    def _first_var(self, root):
        return _find(root, "dataDscr/var")

    # -- <filtrInstr> present ------------------------------------------------

    def test_filter_condition_emits_filtrInstr(self):
        """filter_condition → <filtrInstr> element inside <qstn>."""
        root = self._serialise([{
            "name": "Q4",
            "label": "Q4: 詳細質問",
            "question": "Q3で『はい』と答えた方にお聞きします。",
            "filter_condition": "Q3で『はい』と答えた方のみ",
            "format": "numeric",
        }])
        var_el = self._first_var(root)
        qstn = var_el.find(f"{_NS}qstn")
        self.assertIsNotNone(qstn, "<qstn> should exist")
        filtr = qstn.find(f"{_NS}filtrInstr")
        self.assertIsNotNone(filtr, "<filtrInstr> should exist inside <qstn>")
        self.assertEqual(filtr.text, "Q3で『はい』と答えた方のみ")

    def test_filtrInstr_alongside_qstnLit(self):
        """When both question and filter_condition are set, both elements appear."""
        root = self._serialise([{
            "name": "Q4",
            "label": "Q4",
            "question": "詳細をお聞かせください。",
            "filter_condition": "Q3回答者のみ",
            "format": "string",
        }])
        var_el = self._first_var(root)
        qstn = var_el.find(f"{_NS}qstn")
        qstn_lit = qstn.find(f"{_NS}qstnLit")
        filtr = qstn.find(f"{_NS}filtrInstr")
        self.assertIsNotNone(qstn_lit, "<qstnLit> should exist")
        self.assertEqual(qstn_lit.text, "詳細をお聞かせください。")
        self.assertIsNotNone(filtr, "<filtrInstr> should exist")
        self.assertEqual(filtr.text, "Q3回答者のみ")

    def test_filter_condition_only_no_question(self):
        """filter_condition without question → <qstn> with only <filtrInstr> (no <qstnLit>)."""
        root = self._serialise([{
            "name": "Q5",
            "label": "Q5",
            "question": None,
            "filter_condition": "Q4で『その他』を選んだ方のみ",
            "format": "string",
        }])
        var_el = self._first_var(root)
        qstn = var_el.find(f"{_NS}qstn")
        self.assertIsNotNone(qstn, "<qstn> should exist even without question text")
        qstn_lit = qstn.find(f"{_NS}qstnLit")
        self.assertIsNone(qstn_lit, "<qstnLit> should NOT exist when question is None")
        filtr = qstn.find(f"{_NS}filtrInstr")
        self.assertIsNotNone(filtr, "<filtrInstr> should exist")
        self.assertEqual(filtr.text, "Q4で『その他』を選んだ方のみ")

    def test_english_skip_instruction(self):
        """English-style skip instruction is output correctly."""
        root = self._serialise([{
            "name": "Q5b",
            "label": "Follow-up",
            "question": "Please elaborate.",
            "filter_condition": "Skip to Q5b if Yes to Q5",
            "format": "string",
        }])
        var_el = self._first_var(root)
        qstn = var_el.find(f"{_NS}qstn")
        filtr = qstn.find(f"{_NS}filtrInstr")
        self.assertIsNotNone(filtr)
        self.assertEqual(filtr.text, "Skip to Q5b if Yes to Q5")

    # -- <filtrInstr> absent -------------------------------------------------

    def test_no_filter_condition_no_filtrInstr(self):
        """filter_condition absent → no <filtrInstr> in output."""
        root = self._serialise([{
            "name": "Q1",
            "label": "Q1: 性別",
            "question": "あなたの性別は？",
            "categories": [{"value": "1", "label": "男性"}, {"value": "2", "label": "女性"}],
            "format": "numeric",
        }])
        var_el = self._first_var(root)
        qstn = var_el.find(f"{_NS}qstn")
        self.assertIsNotNone(qstn)
        filtr = qstn.find(f"{_NS}filtrInstr")
        self.assertIsNone(filtr, "<filtrInstr> should NOT exist when filter_condition is absent")

    def test_filter_condition_none_no_filtrInstr(self):
        """filter_condition=None → no <filtrInstr>."""
        root = self._serialise([{
            "name": "Q2",
            "label": "年齢",
            "question": "年齢を入力してください。",
            "filter_condition": None,
            "format": "numeric",
        }])
        var_el = self._first_var(root)
        qstn = var_el.find(f"{_NS}qstn")
        filtr = qstn.find(f"{_NS}filtrInstr")
        self.assertIsNone(filtr)

    def test_no_question_no_filter_no_qstn(self):
        """Both question and filter_condition absent → no <qstn> element at all."""
        root = self._serialise([{
            "name": "Q3",
            "label": "Q3",
            "question": None,
            "filter_condition": None,
            "format": "numeric",
        }])
        var_el = self._first_var(root)
        qstn = var_el.find(f"{_NS}qstn")
        self.assertIsNone(
            qstn,
            "<qstn> should NOT exist when both question and filter_condition are None",
        )

    # -- other fields unaffected ---------------------------------------------

    def test_filter_condition_does_not_affect_label(self):
        """filter_condition does not change the <labl> output."""
        root = self._serialise([{
            "name": "Q4",
            "label": "フィルター付き質問",
            "question": "詳細をお答えください。",
            "filter_condition": "前問で『はい』の方のみ",
            "format": "string",
        }])
        var_el = self._first_var(root)
        labl = var_el.find(f"{_NS}labl")
        self.assertIsNotNone(labl)
        self.assertEqual(labl.text, "フィルター付き質問")

    def test_filter_condition_does_not_affect_categories(self):
        """filter_condition does not affect <catgry> output."""
        root = self._serialise([{
            "name": "Q4",
            "label": "Q4",
            "question": "Q4の質問",
            "filter_condition": "Q3回答者のみ",
            "categories": [{"value": "1", "label": "はい"}, {"value": "2", "label": "いいえ"}],
            "format": "numeric",
        }])
        var_el = self._first_var(root)
        catgry_els = var_el.findall(f"{_NS}catgry")
        self.assertEqual(len(catgry_els), 2)

    def test_multiple_vars_filter_condition_only_on_some(self):
        """Only variables with filter_condition get <filtrInstr>; others do not."""
        root = self._serialise([
            {
                "name": "Q1",
                "label": "Q1",
                "question": "通常の質問",
                "format": "numeric",
            },
            {
                "name": "Q2",
                "label": "Q2",
                "question": "Q1で『はい』の方へ",
                "filter_condition": "Q1で『はい』と答えた方のみ",
                "format": "string",
            },
        ])
        data_dscr = _find(root, "dataDscr")
        vars_els = data_dscr.findall(f"{_NS}var")
        self.assertEqual(len(vars_els), 2)

        # Q1 — no <filtrInstr>
        qstn1 = vars_els[0].find(f"{_NS}qstn")
        self.assertIsNone(qstn1.find(f"{_NS}filtrInstr"))

        # Q2 — has <filtrInstr>
        qstn2 = vars_els[1].find(f"{_NS}qstn")
        filtr2 = qstn2.find(f"{_NS}filtrInstr")
        self.assertIsNotNone(filtr2)
        self.assertEqual(filtr2.text, "Q1で『はい』と答えた方のみ")


# ---------------------------------------------------------------------------
# Tests: paper_to_ddi_agent survey instructions include filter_condition (C2)
# ---------------------------------------------------------------------------

class TestSurveyInstructionsFilterCondition(unittest.TestCase):
    """Verify that EXTRACTION_GROUPS['survey'] instructions mention filter_condition."""

    def test_survey_instructions_mention_filter_condition(self):
        """'filter_condition' key is described in survey extraction instructions."""
        instructions = EXTRACTION_GROUPS["survey"]["instructions"]
        self.assertIn("filter_condition", instructions)

    def test_survey_instructions_describe_skip_pattern(self):
        """Instructions include an example of a skip/filter pattern."""
        instructions = EXTRACTION_GROUPS["survey"]["instructions"]
        self.assertTrue(
            "skip" in instructions.lower() or "filter" in instructions.lower(),
            "Instructions should mention skip or filter behaviour",
        )

    def test_survey_instructions_mention_null_when_unconditional(self):
        """Instructions clarify to use null when question is unconditional."""
        instructions = EXTRACTION_GROUPS["survey"]["instructions"]
        self.assertIn("null", instructions.lower())


if __name__ == "__main__":
    unittest.main()
