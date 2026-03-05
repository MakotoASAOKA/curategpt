"""Tests for C3 – chunked survey-variable extraction (vars_per_block).

Covers:
- _QUESTION_BOUNDARY_RE pattern matching
- _split_survey_blocks() block splitting logic
- _extract_survey_variables_chunked() multi-block LLM calls
- vars_per_block dataclass default
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Minimal stubs so the module can be imported without the full CurateGPT stack
# ---------------------------------------------------------------------------

def _make_stub(name, attrs=None):
    mod = types.ModuleType(name)
    if attrs:
        for k, v in attrs.items():
            setattr(mod, k, v)
    return mod


# Stub heavy dependencies before importing the agent
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

# yaml needs safe_load and dump
yaml_stub = sys.modules["yaml"]
yaml_stub.safe_load = lambda s: {}
yaml_stub.dump = lambda obj, **kw: ""
yaml_stub.YAMLError = Exception

# apply_cv_mappings identity stub
sys.modules["curategpt.wrappers.social.ddi_cv_mapper"].apply_cv_mappings = lambda d: d

# BaseAgent minimal stub
class _BaseAgent:
    knowledge_source = None
    knowledge_source_collection = None
    extractor = None

sys.modules["curategpt.agents.base_agent"].BaseAgent = _BaseAgent

# Load the agent module directly from file path
_AGENT_PATH = (
    Path(__file__).parent.parent.parent
    / "src" / "curategpt" / "agents" / "paper_to_ddi_agent.py"
)
_spec = importlib.util.spec_from_file_location("paper_to_ddi_agent", _AGENT_PATH)
agent_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_mod)
PaperToDDIAgent = agent_mod.PaperToDDIAgent
_QUESTION_BOUNDARY_RE = agent_mod._QUESTION_BOUNDARY_RE
_VARS_PER_BLOCK = agent_mod._VARS_PER_BLOCK


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(**kwargs) -> PaperToDDIAgent:
    """Return a minimal agent with no knowledge source / extractor."""
    agent = object.__new__(PaperToDDIAgent)
    agent.knowledge_source = None
    agent.knowledge_source_collection = None
    agent.extractor = None
    agent.context_tokens = 8000
    agent.rag_example_limit = 3
    agent.enable_source_check = False
    agent.enable_cv_mapping = False
    agent.enable_table_extraction = True
    agent.vars_per_block = kwargs.get("vars_per_block", _VARS_PER_BLOCK)
    return agent


def _make_questionnaire(n_questions: int, prefix: str = "Q", style: str = "plain") -> str:
    """Generate a synthetic questionnaire text with *n_questions* questions.

    style='plain' produces:
        Q1. 質問文1
        1. 選択肢A  2. 選択肢B

    style='pipe' produces (A2 table format):
        | Q1 | 質問文1 | 1. 選択肢A  2. 選択肢B |
    """
    lines = ["調査票\n"]
    for i in range(1, n_questions + 1):
        if style == "pipe":
            lines.append(f"| {prefix}{i} | 質問文{i} | 1. 選択肢A  2. 選択肢B |")
        else:
            lines.append(f"{prefix}{i}. 質問文{i}")
            lines.append("1. 選択肢A  2. 選択肢B")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# _QUESTION_BOUNDARY_RE matching
# ---------------------------------------------------------------------------

class TestQuestionBoundaryRe(unittest.TestCase):

    def _matches(self, line: str) -> bool:
        return bool(_QUESTION_BOUNDARY_RE.search(line))

    def test_english_Q_style(self):
        self.assertTrue(self._matches("Q1. 性別を教えてください"))

    def test_english_Q_space(self):
        self.assertTrue(self._matches("Q 12 年齢"))

    def test_english_F_style(self):
        self.assertTrue(self._matches("F3. 婚姻状況"))

    def test_english_SQ_style(self):
        self.assertTrue(self._matches("SQ2. 追加質問"))

    def test_japanese_mon_style(self):
        self.assertTrue(self._matches("問1　性別"))

    def test_japanese_mon_space(self):
        self.assertTrue(self._matches("問 5. 教育歴"))

    def test_japanese_daikon_style(self):
        self.assertTrue(self._matches("第1問　あなたの性別は？"))

    def test_pipe_table_Q_style(self):
        self.assertTrue(self._matches("| Q1 | 性別 | 1. 男性 2. 女性 |"))

    def test_pipe_table_mon_style(self):
        self.assertTrue(self._matches("| 問3 | 婚姻状況 | 1. 未婚 2. 既婚 |"))

    def test_no_match_plain_text(self):
        self.assertFalse(self._matches("この調査は匿名で実施します。"))

    def test_no_match_option_line(self):
        self.assertFalse(self._matches("1. 男性  2. 女性  3. その他"))

    def test_no_match_header(self):
        self.assertFalse(self._matches("調査票"))


# ---------------------------------------------------------------------------
# _split_survey_blocks
# ---------------------------------------------------------------------------

class TestSplitSurveyBlocks(unittest.TestCase):

    def test_small_questionnaire_single_block(self):
        """Questionnaire with ≤vars_per_block questions → one block (no split)."""
        agent = _make_agent(vars_per_block=50)
        text = _make_questionnaire(30)
        blocks = agent._split_survey_blocks(text)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0], text)

    def test_exactly_at_limit_single_block(self):
        """Exactly vars_per_block questions → one block."""
        agent = _make_agent(vars_per_block=50)
        text = _make_questionnaire(50)
        blocks = agent._split_survey_blocks(text)
        self.assertEqual(len(blocks), 1)

    def test_large_questionnaire_split_into_multiple_blocks(self):
        """Questionnaire with 120 questions and vars_per_block=50 → 3 blocks."""
        agent = _make_agent(vars_per_block=50)
        text = _make_questionnaire(120)
        blocks = agent._split_survey_blocks(text)
        self.assertEqual(len(blocks), 3)

    def test_block_count_100_vars_block_size_50(self):
        """100 questions, vars_per_block=50 → 2 blocks (each 50 questions)."""
        agent = _make_agent(vars_per_block=50)
        text = _make_questionnaire(100)
        blocks = agent._split_survey_blocks(text)
        self.assertEqual(len(blocks), 2)

    def test_no_question_boundaries_single_block(self):
        """Text with no recognisable question patterns → single block."""
        agent = _make_agent(vars_per_block=50)
        text = "この報告書には質問番号がありません。\n一般的なテキストです。"
        blocks = agent._split_survey_blocks(text)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0], text)

    def test_block_2_starts_at_q51(self):
        """Block 2 must start at Q51 (not Q1) when vars_per_block=50."""
        agent = _make_agent(vars_per_block=50)
        text = _make_questionnaire(100)
        blocks = agent._split_survey_blocks(text)
        self.assertIn("Q51", blocks[1])
        self.assertNotIn("Q51", blocks[0])

    def test_preamble_text_in_first_block(self):
        """Text before the first question number goes into block 1."""
        agent = _make_agent(vars_per_block=3)
        preamble = "この調査票に関する説明文\n"
        questions = _make_questionnaire(6)
        text = preamble + questions
        blocks = agent._split_survey_blocks(text)
        self.assertIn("この調査票に関する説明文", blocks[0])

    def test_custom_vars_per_block(self):
        """vars_per_block=10 → questionnaire with 25 questions splits into 3 blocks."""
        agent = _make_agent(vars_per_block=10)
        text = _make_questionnaire(25)
        blocks = agent._split_survey_blocks(text)
        self.assertEqual(len(blocks), 3)

    def test_pipe_table_format_split(self):
        """Pipe-delimited A2 format questionnaire with 100 questions → 2 blocks."""
        agent = _make_agent(vars_per_block=50)
        text = _make_questionnaire(100, style="pipe")
        blocks = agent._split_survey_blocks(text)
        self.assertEqual(len(blocks), 2)

    def test_japanese_mon_format_split(self):
        """問N-style questionnaire with 60 questions → 2 blocks."""
        agent = _make_agent(vars_per_block=50)
        text = _make_questionnaire(60, prefix="問")
        blocks = agent._split_survey_blocks(text)
        self.assertEqual(len(blocks), 2)


# ---------------------------------------------------------------------------
# _extract_survey_variables_chunked
# ---------------------------------------------------------------------------

class TestExtractSurveyVariablesChunked(unittest.TestCase):

    def _mock_llm_response(self, variables_per_call: list) -> MagicMock:
        """Return a mock extractor whose model.prompt() yields successive YAML responses."""
        import yaml as real_yaml  # noqa: F401 – use internal yaml stub

        responses = iter(variables_per_call)

        def _prompt_side_effect(prompt_text):
            vars_list = next(responses)
            # Produce a real YAML string the agent can parse
            import yaml as _yaml_mod
            _yaml_mod.safe_load = lambda s: {"variables": vars_list}
            mock_resp = MagicMock()
            mock_resp.text.return_value = "variables:\n- name: dummy"
            return mock_resp

        mock_model = MagicMock()
        mock_model.prompt.side_effect = _prompt_side_effect
        mock_extractor = MagicMock()
        mock_extractor.model = mock_model
        return mock_extractor

    def test_single_block_uses_standard_path(self):
        """When only 1 block, _extract_group is called once."""
        agent = _make_agent(vars_per_block=50)

        called = []

        def fake_extract_group(**kwargs):
            called.append(kwargs["group_name"])
            return {"variables": [{"name": "Q1", "label": "性別"}]}

        agent._extract_group = fake_extract_group
        text = _make_questionnaire(10)
        vars_ = agent._extract_survey_variables_chunked(text, "instructions")

        self.assertEqual(called, ["survey"])
        self.assertEqual(len(vars_), 1)
        self.assertEqual(vars_[0]["name"], "Q1")

    def test_multiple_blocks_calls_llm_per_block(self):
        """With 3 blocks, _extract_group is called 3 times and variables combined."""
        agent = _make_agent(vars_per_block=10)
        text = _make_questionnaire(25)

        call_log = []

        def fake_extract_group(**kwargs):
            call_log.append(kwargs["group_name"])
            # Each block returns 5 mock variables
            return {"variables": [{"name": f"V{i}", "label": "x"} for i in range(5)]}

        agent._extract_group = fake_extract_group
        vars_ = agent._extract_survey_variables_chunked(text, "instructions")

        # 3 blocks → 3 LLM calls → 15 variables total
        self.assertEqual(len(call_log), 3)
        self.assertIn("survey_block_1", call_log)
        self.assertIn("survey_block_2", call_log)
        self.assertIn("survey_block_3", call_log)
        self.assertEqual(len(vars_), 15)

    def test_empty_block_response_skipped(self):
        """Blocks returning empty variables list do not cause errors."""
        agent = _make_agent(vars_per_block=10)
        text = _make_questionnaire(25)

        responses = [
            {"variables": [{"name": "Q1"}]},
            {},                              # missing key → no vars
            {"variables": None},             # None → no vars
        ]
        resp_iter = iter(responses)

        def fake_extract_group(**kwargs):
            return next(resp_iter)

        agent._extract_group = fake_extract_group
        vars_ = agent._extract_survey_variables_chunked(text, "instructions")
        self.assertEqual(len(vars_), 1)

    def test_vars_per_block_default_is_50(self):
        """Default vars_per_block equals the module constant _VARS_PER_BLOCK."""
        agent = _make_agent()
        self.assertEqual(agent.vars_per_block, _VARS_PER_BLOCK)
        self.assertEqual(_VARS_PER_BLOCK, 50)


if __name__ == "__main__":
    unittest.main()
