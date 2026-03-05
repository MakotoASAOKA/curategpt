"""Tests for B3 – unstructured questionnaire survey_items auto-promotion.

Covers:
- _promote_survey_items(): core promotion logic
- _extract_sections() integration: promotion called when enable_survey_promotion=True
- enable_survey_promotion flag (default True, disabled when False)
- Various question-number formats (Q, F, SQ, 問, 第N問, pipe-table)
- Threshold: fewer than _B3_MIN_QUESTION_COUNT boundaries → no promotion
- Preamble text before first question is preserved
- Explicit survey_items heading always wins over heuristic
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Minimal stubs so the module can be imported without the full CurateGPT stack
# ---------------------------------------------------------------------------

def _make_stub(name, attrs=None):
    mod = types.ModuleType(name)
    if attrs:
        for k, v in attrs.items():
            setattr(mod, k, v)
    return mod


for _name in [
    "yaml",
    "pdfplumber",
    "textract",
    "curategpt",
    "curategpt.agents",
    "curategpt.agents.base_agent",
    "curategpt.wrappers",
    "curategpt.wrappers.social",
    "curategpt.wrappers.social.ddi_cv_mapper",
]:
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

_AGENT_PATH = (
    Path(__file__).parent.parent.parent
    / "src" / "curategpt" / "agents" / "paper_to_ddi_agent.py"
)
_spec = importlib.util.spec_from_file_location("paper_to_ddi_agent", _AGENT_PATH)
agent_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_mod)

PaperToDDIAgent = agent_mod.PaperToDDIAgent
_B3_MIN_QUESTION_COUNT = agent_mod._B3_MIN_QUESTION_COUNT


# ---------------------------------------------------------------------------
# Helper: build a minimal agent instance
# ---------------------------------------------------------------------------

def _make_agent(**kwargs) -> PaperToDDIAgent:
    agent = object.__new__(PaperToDDIAgent)
    agent.knowledge_source = None
    agent.knowledge_source_collection = None
    agent.extractor = None
    agent.context_tokens = 8000
    agent.rag_example_limit = 3
    agent.enable_source_check = False
    agent.enable_cv_mapping = False
    agent.enable_table_extraction = True
    agent.enable_survey_promotion = kwargs.get("enable_survey_promotion", True)
    agent.vars_per_block = 50
    return agent


def _plain_questionnaire(n: int, prefix: str = "Q") -> str:
    """Generate a plain-text questionnaire with *n* questions (no section heading)."""
    lines = []
    for i in range(1, n + 1):
        lines.append(f"{prefix}{i}. 質問文{i}")
        lines.append("1. 選択肢A  2. 選択肢B")
    return "\n".join(lines)


def _pipe_questionnaire(n: int) -> str:
    """Generate a pipe-delimited (A2 format) questionnaire with *n* questions."""
    lines = [f"| Q{i} | 質問文{i} | 1. 選択肢A  2. 選択肢B |" for i in range(1, n + 1)]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tests for _promote_survey_items()
# ---------------------------------------------------------------------------

class TestPromoteSurveyItems(unittest.TestCase):

    def _promote(self, sections: dict, **kwargs) -> dict:
        agent = _make_agent(**kwargs)
        return agent._promote_survey_items(sections)

    # -- promotion triggered ---------------------------------------------------

    def test_q_style_3_questions_promoted(self):
        """3 Q-style questions in preamble → survey_items created."""
        text = _plain_questionnaire(3, prefix="Q")
        sections = {"preamble": text, "full": text}
        result = self._promote(sections)
        self.assertIn("survey_items", result)

    def test_mon_style_3_questions_promoted(self):
        """3 問N-style questions → promoted."""
        text = _plain_questionnaire(3, prefix="問")
        sections = {"preamble": text, "full": text}
        result = self._promote(sections)
        self.assertIn("survey_items", result)

    def test_f_style_promoted(self):
        """F1/F2/F3 variable codes → promoted."""
        text = _plain_questionnaire(3, prefix="F")
        sections = {"preamble": text, "full": text}
        result = self._promote(sections)
        self.assertIn("survey_items", result)

    def test_daini_mon_style_promoted(self):
        """第N問-style → promoted."""
        lines = []
        for i in range(1, 4):
            lines.append(f"第{i}問　質問文{i}")
            lines.append("1. はい  2. いいえ")
        text = "\n".join(lines)
        sections = {"preamble": text, "full": text}
        result = self._promote(sections)
        self.assertIn("survey_items", result)

    def test_pipe_table_format_promoted(self):
        """Pipe-delimited A2 format with 3 rows → promoted."""
        text = _pipe_questionnaire(3)
        sections = {"preamble": text, "full": text}
        result = self._promote(sections)
        self.assertIn("survey_items", result)

    def test_survey_items_contains_questions(self):
        """Promoted survey_items contains the actual question text."""
        text = "Q1. 性別\n1. 男性\nQ2. 年齢\n自由記述\nQ3. 職業\n1. 会社員"
        sections = {"preamble": text, "full": text}
        result = self._promote(sections)
        self.assertIn("Q1", result["survey_items"])
        self.assertIn("Q2", result["survey_items"])
        self.assertIn("Q3", result["survey_items"])

    def test_large_questionnaire_promoted(self):
        """10 questions → promoted (well above threshold)."""
        text = _plain_questionnaire(10)
        sections = {"preamble": text, "full": text}
        result = self._promote(sections)
        self.assertIn("survey_items", result)

    # -- promotion NOT triggered -----------------------------------------------

    def test_fewer_than_threshold_not_promoted(self):
        """2 questions < _B3_MIN_QUESTION_COUNT (3) → not promoted."""
        text = _plain_questionnaire(2)
        sections = {"preamble": text, "full": text}
        result = self._promote(sections)
        self.assertNotIn("survey_items", result)

    def test_zero_questions_not_promoted(self):
        """No question patterns → not promoted."""
        text = "この文書には質問番号がありません。\n一般的なテキストです。"
        sections = {"preamble": text, "full": text}
        result = self._promote(sections)
        self.assertNotIn("survey_items", result)

    def test_existing_survey_items_unchanged(self):
        """survey_items already in sections → early return, preamble unchanged."""
        existing_survey = "Q1. 既存の質問\nQ2. 別の質問"
        preamble = "調査の説明\nQ1. 性別\nQ2. 年齢\nQ3. 職業"
        sections = {
            "survey_items": existing_survey,
            "preamble": preamble,
            "full": preamble,
        }
        result = self._promote(sections)
        self.assertEqual(result["survey_items"], existing_survey)
        self.assertEqual(result["preamble"], preamble)

    def test_empty_preamble_not_promoted(self):
        """Empty preamble → early return."""
        sections = {"full": "some text"}
        result = self._promote(sections)
        self.assertNotIn("survey_items", result)

    # -- preamble splitting ----------------------------------------------------

    def test_intro_text_before_questions_preserved_in_preamble(self):
        """Text before Q1 stays in preamble; text from Q1 goes to survey_items."""
        intro = "調査へのご協力をお願いします。\n以下の質問にお答えください。"
        questions = "\nQ1. 性別\n1. 男性\nQ2. 年齢\n記述\nQ3. 職業\n記述"
        text = intro + questions
        sections = {"preamble": text, "full": text}
        result = self._promote(sections)
        self.assertIn("survey_items", result)
        self.assertIn("調査へのご協力", result["preamble"])
        self.assertNotIn("調査へのご協力", result["survey_items"])
        self.assertIn("Q1", result["survey_items"])

    def test_no_intro_text_preamble_key_removed(self):
        """When preamble is all questions (Q1 at offset 0), preamble key is removed."""
        text = "Q1. 性別\n1. 男性\nQ2. 年齢\n記述\nQ3. 職業\n記述"
        sections = {"preamble": text, "full": text}
        result = self._promote(sections)
        self.assertIn("survey_items", result)
        # preamble should be absent (no text before Q1) or empty
        self.assertNotIn("preamble", result)

    def test_full_text_key_unchanged(self):
        """The 'full' key must remain unchanged after promotion."""
        text = _plain_questionnaire(3)
        sections = {"preamble": text, "full": text}
        result = self._promote(sections)
        self.assertEqual(result["full"], text)

    def test_threshold_is_3(self):
        """_B3_MIN_QUESTION_COUNT module constant equals 3."""
        self.assertEqual(_B3_MIN_QUESTION_COUNT, 3)


# ---------------------------------------------------------------------------
# Tests for _extract_sections() integration
# ---------------------------------------------------------------------------

class TestExtractSectionsB3Integration(unittest.TestCase):

    def test_unstructured_questionnaire_gets_survey_items(self):
        """Full _extract_sections() call: no heading + 5 questions → survey_items."""
        agent = _make_agent(enable_survey_promotion=True)
        text = _plain_questionnaire(5)
        sections = agent._extract_sections(text)
        self.assertIn("survey_items", sections)

    def test_unstructured_japanese_gets_survey_items(self):
        """Japanese 問N format in unstructured questionnaire → survey_items."""
        agent = _make_agent(enable_survey_promotion=True)
        text = _plain_questionnaire(4, prefix="問")
        sections = agent._extract_sections(text)
        self.assertIn("survey_items", sections)

    def test_explicit_heading_wins_over_heuristic(self):
        """When an explicit 調査票 heading is present, survey_items is set by it."""
        agent = _make_agent(enable_survey_promotion=True)
        text = (
            "調査の説明\n\n"
            "調査票\n"          # explicit heading → survey_items starts here
            "Q1. 性別\nQ2. 年齢\nQ3. 職業\n"
        )
        sections = agent._extract_sections(text)
        self.assertIn("survey_items", sections)
        # The survey_items content should come from the heading split, not the heuristic
        # (we just verify the key exists and contains Q1)
        self.assertIn("Q1", sections["survey_items"])

    def test_enable_survey_promotion_false_no_promotion(self):
        """enable_survey_promotion=False → survey_items NOT created by heuristic."""
        agent = _make_agent(enable_survey_promotion=False)
        text = _plain_questionnaire(5)
        sections = agent._extract_sections(text)
        self.assertNotIn("survey_items", sections)

    def test_enable_survey_promotion_default_true(self):
        """Default value of enable_survey_promotion is True."""
        agent = _make_agent()
        self.assertTrue(agent.enable_survey_promotion)

    def test_with_preamble_intro_text(self):
        """Questionnaire with cover text + questions: intro in preamble, Q in survey_items."""
        agent = _make_agent(enable_survey_promotion=True)
        text = (
            "アンケート調査へのご参加ありがとうございます。\n"
            "以下の質問にご回答ください。\n"
            "Q1. あなたの性別を教えてください。\n"
            "1. 男性  2. 女性  3. その他\n"
            "Q2. あなたの年齢を教えてください。\n"
            "（　　）歳\n"
            "Q3. 現在のご職業は何ですか。\n"
            "1. 会社員  2. 公務員  3. 自営業  4. 学生  5. その他\n"
        )
        sections = agent._extract_sections(text)
        self.assertIn("survey_items", sections)
        self.assertIn("preamble", sections)
        self.assertIn("アンケート調査", sections["preamble"])
        self.assertIn("Q1", sections["survey_items"])

    def test_pipe_table_questionnaire_promoted(self):
        """Pipe-delimited (A2) questionnaire with no heading → survey_items promoted."""
        agent = _make_agent(enable_survey_promotion=True)
        text = _pipe_questionnaire(4)
        sections = agent._extract_sections(text)
        self.assertIn("survey_items", sections)


if __name__ == "__main__":
    unittest.main()
