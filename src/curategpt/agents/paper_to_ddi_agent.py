"""Section-aware, two-pass DDI-Codebook extraction agent.

Problem addressed
-----------------
:class:`~curategpt.wrappers.general.filesystem_wrapper.FilesystemWrapper`
splits documents every 3 000 characters, which destroys cross-section context
(e.g. the methodology section never sees the title page).

Solution
--------
This agent:

1. **Pass 1 – Section detection**: splits the full document into named sections
   (abstract, methods, participants, survey items, …) using regex heuristics
   for both English and Japanese academic paper conventions.

2. **Pass 2 – Group extraction**: groups the sections into three thematic
   "extraction groups" (bibliographic, methodology, survey) and calls the LLM
   once per group.  Each call receives:

   * A token-budget-aware excerpt of the relevant section text.
   * RAG examples retrieved from the existing DDI knowledge base (if any).

The results of all groups are merged into a single DDI metadata dict.

Typical usage — single file
----------------------------
::

    from curategpt.store import ChromaDBAdapter
    from curategpt.extract.basic_extractor import BasicExtractor
    from curategpt.agents.paper_to_ddi_agent import PaperToDDIAgent
    import llm

    store = ChromaDBAdapter("path/to/db")
    extractor = BasicExtractor()
    extractor.model = llm.get_model("gpt-4o")

    agent = PaperToDDIAgent(
        knowledge_source=store,
        knowledge_source_collection="ddi_gesis",
        extractor=extractor,
    )

    ddi = agent.extract_ddi_from_file("survey_paper.pdf")
    print(ddi["title"])

Typical usage — multiple files (D1)
-------------------------------------
::

    # Pass a dict mapping role → file path.
    # Supported roles:
    #   "report"        – bibliographic + methodology groups
    #   "questionnaire" – survey group (variables only)
    #   "all"           – all groups (same as single-file extraction)
    #
    # Or pass a list of (role, path) tuples to handle multiple files
    # with the same role.

    ddi = agent.extract_ddi_from_files({
        "report":        "survey_report.pdf",
        "questionnaire": "questionnaire.pdf",
    })
    print(ddi["title"])
    print(len(ddi.get("variables", [])))
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Tuple, Union

import yaml

from curategpt.agents.base_agent import BaseAgent
from curategpt.wrappers.social.ddi_cv_mapper import apply_cv_mappings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Section detection patterns
# English headings use case-insensitive full-line match.
# Japanese headings added as literal alternatives.
# ---------------------------------------------------------------------------

# Regex that strips a leading numbering prefix from a heading line so that
# patterns like r"(?i)^methods?\s*$" also match numbered headings such as:
#   "1. Methods"    "3.1 Methods"   "II. Methods"   "A) Methods"
#   "第2章 方法"     "第一節 方法"
# The prefix is stripped before pattern matching; the original line is also
# tried first so that patterns explicitly targeting the full line still work.
_NUMBERED_HEADING_PREFIX = re.compile(
    r"""^
    (?:
        # Japanese chapter/section markers: 第1章, 第二節, 第１部, etc.
        第 \s* [0-9０-９一二三四五六七八九十百千]+  # 第1 / 第一
        \s* [章節部]                              # 章 / 節 / 部
        \s*
        |
        # Roman numerals followed by punctuation or space: I. II) III: IV
        [IVXivx]{1,8} [\.\):\s] \s*
        |
        # Arabic numerals, optionally dotted sub-sections: 1. 2) 3: 1.2. 3.1
        [0-9]{1,3} (?: \. [0-9]{1,3} )* [\.\):\s] \s*
        |
        # Single letter prefix: A. B)
        [A-Za-z] [\.\)] \s*
    )""",
    re.VERBOSE,
)

SECTION_PATTERNS: Dict[str, List[str]] = {
    "abstract": [
        r"(?i)^abstract\s*$",
        r"(?i)^summary\s*$",
        r"^要旨\s*$",
        r"^概要\s*$",
        r"^抄録\s*$",
    ],
    "introduction": [
        r"(?i)^(introduction|background)\s*$",
        r"^(はじめに|序章|序論|背景)\s*$",
    ],
    "methods": [
        r"(?i)^(methods?|methodology|research design|study design|procedure)\s*$",
        r"^(方法|調査方法|研究方法|研究デザイン|手続き)\s*$",
    ],
    "participants": [
        r"(?i)^(participants?|subjects?|sample|respondents?)\s*$",
        r"^(対象者|調査対象|サンプル|回答者)\s*$",
    ],
    "data_collection": [
        r"(?i)^(data collection|data gathering|fieldwork|survey administration)\s*$",
        r"^(データ収集|調査実施|フィールドワーク)\s*$",
    ],
    "survey_items": [
        r"(?i)^(questionnaire|survey items?|interview schedule|questions?)\s*$",
        r"^(調査票|質問票|質問紙|インタビュースケジュール|質問項目)\s*$",
        r"(?i)^appendix\s*(a|b|1|2|:)?\s*(questionnaire|survey)?\s*$",
        r"^付録\s*(調査票|質問票)?\s*$",
    ],
    "results": [
        r"(?i)^(results?|findings?)\s*$",
        r"^(結果|分析結果|知見)\s*$",
    ],
    "discussion": [
        r"(?i)^(discussion|conclusion|conclusions?)\s*$",
        r"^(考察|議論|結論)\s*$",
    ],
    "references": [
        r"(?i)^(references?|bibliography)\s*$",
        r"^(参考文献|文献|引用文献)\s*$",
    ],
}

# Sections to skip when building extraction prompts (not useful for DDI)
_SKIP_SECTIONS = {"results", "discussion", "references"}

# ---------------------------------------------------------------------------
# Mapping: DDI field → section group name
# ---------------------------------------------------------------------------

# Sections fed to each extraction group
EXTRACTION_GROUPS: Dict[str, Dict[str, Any]] = {
    "bibliographic": {
        "sections": ["preamble", "abstract", "introduction"],
        "fields": [
            "title",
            "alternative_title",
            "abstract",
            "principal_investigators",
            "funding_agencies",
            "distributors",
            "study_ids",
            "keywords",
            "topic_classification",
        ],
        "instructions": (
            "Extract bibliographic and descriptive metadata. "
            "'principal_investigators' should be a list of full names as written in the text. "
            "'keywords' and 'topic_classification' should each be a list of strings. "
            "IMPORTANT — 'study_ids': must be an official archive identifier "
            "(e.g., ZA1234, UKDA-6789, e-Stat:00001) EXPLICITLY PRINTED in the document. "
            "Do NOT invent one if none is present — return null. "
            "IMPORTANT — 'funding_agencies': must be named in a funding statement or "
            "acknowledgement section. Do NOT infer from institutional affiliation "
            "or guess from general knowledge — return null if absent. "
            "Return null for any other field that cannot be determined from the text."
        ),
    },
    "methodology": {
        "sections": ["methods", "participants", "data_collection"],
        "fields": [
            "time_periods",
            "collection_dates",
            "nations",
            "geographic_coverage",
            "universe",
            "analysis_unit",
            "sampling_procedure",
            "collection_mode",
            "time_method",
            "collection_situation",
            "weighting",
        ],
        "instructions": (
            "Extract methodology and coverage metadata. "
            "'time_periods' and 'collection_dates' should be lists of dicts with "
            "keys 'event' (start|end|single) and 'date' (ISO format if possible). "
            "IMPORTANT — 'collection_dates': use ONLY dates explicitly stated in the text. "
            "Do NOT derive dates from the publication year, copyright year, or vague "
            "period descriptions — return null if no concrete dates are present. "
            "'collection_mode' describes how data was collected (e.g. self-administered "
            "questionnaire, face-to-face interview, online survey). "
            "Return null for any field that cannot be determined."
        ),
    },
    "survey": {
        "sections": ["survey_items"],
        "fields": ["variables"],
        "instructions": (
            "Extract survey variables from the questionnaire section. "
            "'variables' should be a list of dicts, each with keys: "
            "'name' (variable code or short label), "
            "'label' (full question label), "
            "'question' (verbatim question text if available), "
            "'categories' (list of {value, label} dicts for coded responses), "
            "'format' (numeric|string|date), "
            "'filter_condition' (string describing the skip/filter instruction for this "
            "variable, e.g. \"Q3で『はい』と答えた方のみ\" or \"Skip to Q5 if No\"; "
            "null if the question is asked unconditionally). "
            "Extract ALL variables present in the provided text — do not skip any. "
            "Return an empty list if no structured variables are found."
        ),
    },
}

# ---------------------------------------------------------------------------
# Token budget constants
# ---------------------------------------------------------------------------

# Safe character-count proxy: 1 token ≈ 4 chars for English/Japanese mixed text
_CHARS_PER_TOKEN = 4
# Reserve tokens for prompt overhead + examples + response
_RESERVED_TOKENS = 1500
# Default context window (conservative; overridden by model when possible)
_DEFAULT_CONTEXT_TOKENS = 8000


def _char_budget(context_tokens: int) -> int:
    return (context_tokens - _RESERVED_TOKENS) * _CHARS_PER_TOKEN


# ---------------------------------------------------------------------------
# Hallucination countermeasures (C1)
# ---------------------------------------------------------------------------

# Mandatory anti-hallucination block prepended to EVERY LLM extraction prompt.
# Addresses the most common failure mode: the LLM inventing plausible-sounding
# values (archive IDs, funding bodies, dates) that are absent from the source.
_HALLUCINATION_GUARD = """\
EXTRACTION RULES — mandatory, no exceptions:
1. Extract ONLY information that is EXPLICITLY present in the text provided below.
2. Do NOT use your general world knowledge, make inferences, or generate
   plausible-sounding values for missing information.
3. Do NOT invent identifiers, dates, personal names, or institution names
   that do not appear verbatim (or near-verbatim) in the text.
4. If a field is not clearly stated in the text, output null — never guess.
5. When in doubt, prefer null over an uncertain value.
"""

# Fields whose scalar / list-of-string values are cross-checked against the
# source text after extraction.  Items not found as a substring in the source
# are logged as warnings and removed.  Dict-valued list items (e.g. variables,
# time_periods) are exempt from this check because they are assembled by the
# LLM from multiple text fragments and rarely appear verbatim.
_SOURCE_CHECK_FIELDS: frozenset = frozenset(
    {
        "study_ids",
        "funding_agencies",
        "distributors",
        "alternative_title",
    }
)

# ---------------------------------------------------------------------------
# C3 — Chunked survey-variable extraction
# ---------------------------------------------------------------------------

# Default maximum number of survey questions to extract per LLM call.
# The questionnaire text is split into consecutive blocks at detected question
# boundaries before this many questions are reached.  Multiple LLM calls are
# made (one per block) and the resulting variable lists are concatenated.
_VARS_PER_BLOCK: int = 50

# Regex to detect the *start of a new question / variable entry* in survey text.
# Handles both plain-text questionnaire numbering and the pipe-delimited table
# rows produced by the A2 table-extraction path.
#
# Supported patterns (line-anchored, multiline):
#   Q1, Q 1, q1          — English-style question numbers
#   F1, F 2              — Form-variable codes
#   SQ1, SQ 3            — Sub-question codes
#   問1, 問 2            — Japanese-style question numbers
#   第1問, 第 2 問       — Formal Japanese question numbers
#   | Q1 |, | 問1 |     — A2 pipe-delimited table rows (leading pipe)
_QUESTION_BOUNDARY_RE = re.compile(
    r"(?m)^"                              # line start (multiline mode)
    r"(?:\|\s*)?"                         # optional leading pipe (A2 table rows)
    r"(?:"
    r"(?:SQ|F|Q|q)\s*\d+"               # Q1, F2, SQ3, q4
    r"|問\s*\d+"                          # 問1, 問 2
    r"|第\s*\d+\s*問"                    # 第1問, 第 2 問
    r")"
    r"(?=[\s\.\)．。\|]|$)",             # lookahead: separator, pipe, or EOL
)

# B3: Minimum number of question boundaries that must be found in the preamble
# before the content is automatically promoted to a synthetic survey_items
# section.  A threshold > 1 prevents false positives from documents that
# mention question codes in running text (e.g. "Q1 addresses demographics").
_B3_MIN_QUESTION_COUNT: int = 3

# ---------------------------------------------------------------------------
# Role → extraction group mapping  (D1 – multi-file support)
# ---------------------------------------------------------------------------

# Supported role names for extract_ddi_from_files().
# Role strings are matched by prefix so "report_2" also maps to ["bibliographic",
# "methodology"], and "questionnaire_wave2" maps to ["survey"].
_ROLE_GROUPS: Dict[str, List[str]] = {
    "report":        ["bibliographic", "methodology"],
    "questionnaire": ["survey"],
    "all":           list(EXTRACTION_GROUPS.keys()),
}


def _resolve_role(role: str) -> List[str]:
    """Return the group list for *role*, using prefix matching as fallback."""
    if role in _ROLE_GROUPS:
        return _ROLE_GROUPS[role]
    for key in _ROLE_GROUPS:
        if role.startswith(key):
            return _ROLE_GROUPS[key]
    # Unknown role → run all groups (safe default)
    logger.warning("Unknown file role %r — running all extraction groups.", role)
    return list(EXTRACTION_GROUPS.keys())


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


@dataclass
class PaperToDDIAgent(BaseAgent):
    """Extract DDI-Codebook metadata from full research papers or questionnaires.

    Parameters
    ----------
    knowledge_source:
        A :class:`~curategpt.store.DBAdapter` containing previously indexed
        DDI records.  Used for RAG example retrieval.  May be *None* when no
        examples are available (cold-start mode).
    knowledge_source_collection:
        Collection name inside *knowledge_source*.
    extractor:
        A :class:`~curategpt.extract.Extractor` wrapping an LLM.
    context_tokens:
        Approximate context-window size for the LLM.  Used to compute the
        character budget for section text passed to the LLM.
    rag_example_limit:
        Maximum number of RAG examples to prepend to each extraction call.
    """

    name: ClassVar[str] = "paper_to_ddi"

    context_tokens: int = _DEFAULT_CONTEXT_TOKENS
    rag_example_limit: int = 3
    #: When True, extracted scalar/list-of-string values in
    #: :data:`_SOURCE_CHECK_FIELDS` are cross-checked against the source text
    #: after each extraction.  Values not found as a substring in the source
    #: are logged at WARNING level and removed (set to null).  Disable if the
    #: cross-check is too aggressive for your documents.
    enable_source_check: bool = True
    #: When True, free-text vocabulary fields (collection_mode, time_method,
    #: analysis_unit, sampling_procedure) are mapped to DDI Alliance CV codes
    #: by :func:`~curategpt.wrappers.social.ddi_cv_mapper.apply_cv_mappings`.
    #: The original values are preserved; ``{field}_cv`` keys are added.
    enable_cv_mapping: bool = True
    #: When True, PDF pages are scanned for tables using pdfplumber's
    #: ``find_tables()`` API.  Detected tables are formatted as pipe-delimited
    #: text to preserve column order.  Regions outside tables are extracted
    #: separately via ``page.crop()``.  Disable if table detection is too
    #: aggressive for a particular document.
    enable_table_extraction: bool = True
    #: When True, the preamble text is scanned for question-number patterns
    #: (Q1, F1, SQ1, 問1, 第1問, pipe-table rows) after normal heading-based
    #: section detection.  If at least :data:`_B3_MIN_QUESTION_COUNT`
    #: boundaries are found and no ``survey_items`` heading was detected, the
    #: preamble is split at the first boundary: text before the first question
    #: stays in ``preamble``; text from the first question becomes
    #: ``survey_items``.  Disable if the heuristic causes incorrect splits for
    #: a particular document (e.g. a report that lists question codes in text).
    enable_survey_promotion: bool = True
    #: Maximum number of survey questions to extract per LLM call (C3).
    #: When the questionnaire text contains more detected question boundaries
    #: than this value, it is automatically split into consecutive blocks and
    #: the LLM is called once per block.  The resulting ``variables`` lists
    #: are concatenated.  Increase for models with larger context windows.
    vars_per_block: int = _VARS_PER_BLOCK

    def extract_ddi_from_file(self, file_path: str) -> Dict[str, Any]:
        """Read *file_path* and return a DDI metadata dict.

        The file is read in full (via pdfplumber / textract) and then passed
        to :meth:`extract_ddi_from_text`.
        """
        text = self._read_full_document(file_path)
        return self.extract_ddi_from_text(text, filename=Path(file_path).name)

    def extract_ddi_from_files(
        self,
        files: Union[Dict[str, str], List[Tuple[str, str]]],
    ) -> Dict[str, Any]:
        """Extract and merge DDI metadata from **multiple files**.

        Each file is assigned a *role* that determines which extraction groups
        are run against it.  After per-file extraction (and source-check), all
        partial results are merged into a single DDI metadata dict.

        Parameters
        ----------
        files:
            Mapping of **role → file path**, or a list of ``(role, path)``
            tuples (use the list form when the same role applies to more than
            one file, e.g. two questionnaire waves).

            Supported roles:

            ``"report"``
                Runs the *bibliographic* and *methodology* extraction groups.
                Use for survey reports, codebooks, and similar documents that
                contain study-level descriptive and methodological information.

            ``"questionnaire"``
                Runs the *survey* group only (variable extraction).
                Use for questionnaire/interview-schedule files.

            ``"all"``
                Runs all three groups.  Equivalent to
                :meth:`extract_ddi_from_file`.

            Role names are matched by **prefix**, so ``"report_2019"`` is
            treated as ``"report"`` and ``"questionnaire_wave2"`` as
            ``"questionnaire"``.  Unknown roles fall back to ``"all"``.

        Returns
        -------
        dict
            Merged DDI metadata dict.  When the same scalar field is extracted
            from more than one file, the **first** value encountered wins
            (files are processed in the order supplied).  List-valued fields
            (e.g. ``variables``) are concatenated and de-duplicated.

        Example
        -------
        ::

            ddi = agent.extract_ddi_from_files({
                "report":        "survey_report_2023.pdf",
                "questionnaire": "questionnaire_2023.pdf",
            })

            # Multiple files of the same role:
            ddi = agent.extract_ddi_from_files([
                ("report",        "report_wave1.pdf"),
                ("report",        "report_wave2.pdf"),
                ("questionnaire", "questionnaire.pdf"),
            ])
        """
        # Normalise to list of (role, path) tuples
        if isinstance(files, dict):
            file_list: List[Tuple[str, str]] = list(files.items())
        else:
            file_list = list(files)

        if not file_list:
            logger.warning("extract_ddi_from_files: empty file list — returning {}.")
            return {}

        all_partials: List[Dict[str, Any]] = []

        for role, file_path in file_list:
            group_names = _resolve_role(role)
            logger.info(
                "Processing file '%s' with role=%r groups=%s",
                file_path, role, group_names,
            )
            text = self._read_full_document(file_path)
            partial = self._run_extraction_groups(
                text=text,
                filename=Path(file_path).name,
                group_names=group_names,
            )
            all_partials.append(partial)

        merged = self._merge_extractions(all_partials)

        # CV mapping applied once after all files are merged
        if self.enable_cv_mapping:
            merged = apply_cv_mappings(merged)

        # Fallback title from the first report/all file
        if not merged.get("title"):
            for role, file_path in file_list:
                if _resolve_role(role) != _ROLE_GROUPS["questionnaire"]:
                    merged["title"] = Path(file_path).stem
                    break

        return merged

    def extract_ddi_from_text(
        self, text: str, filename: str = ""
    ) -> Dict[str, Any]:
        """Extract DDI metadata from a complete document string.

        Parameters
        ----------
        text:
            Full document text (no pre-chunking).
        filename:
            Optional filename used for logging / fallback *title* generation.

        Returns
        -------
        dict
            Flat DDI metadata dict with fields matching
            :class:`~curategpt.wrappers.social.ddi_codebook_wrapper.DDICodebookWrapper`.
        """
        merged = self._run_extraction_groups(
            text=text,
            filename=filename,
            group_names=list(EXTRACTION_GROUPS.keys()),
        )

        # C4: map free-text vocabulary fields to DDI Alliance CV codes
        if self.enable_cv_mapping:
            merged = apply_cv_mappings(merged)

        # Fallback: use filename as title when not extracted
        if not merged.get("title") and filename:
            merged["title"] = Path(filename).stem

        return merged

    # ------------------------------------------------------------------
    # Internal: role-filtered extraction (shared by single- and multi-file)
    # ------------------------------------------------------------------

    def _run_extraction_groups(
        self,
        text: str,
        filename: str = "",
        group_names: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Run the specified extraction groups against *text*.

        Performs section detection, per-group LLM calls, and per-file
        hallucination source-check.  Does **not** apply CV mappings (so
        that multi-file callers can apply them once after merging).

        Parameters
        ----------
        text:
            Full document text.
        filename:
            Used for logging only.
        group_names:
            List of group names to run (subset of :data:`EXTRACTION_GROUPS`).
            Defaults to all groups when *None*.

        Returns
        -------
        dict
            Partial DDI metadata dict (no ``_cv`` keys).
        """
        if group_names is None:
            group_names = list(EXTRACTION_GROUPS.keys())

        logger.info(
            "DDI extraction for '%s' (%d chars) — groups: %s",
            filename, len(text), group_names,
        )

        sections = self._extract_sections(text)
        logger.debug("Detected sections: %s", list(sections.keys()))

        group_results: List[Dict[str, Any]] = []
        for group_name in group_names:
            group_cfg = EXTRACTION_GROUPS.get(group_name)
            if not group_cfg:
                logger.warning("Unknown extraction group %r — skipping.", group_name)
                continue

            section_text = self._build_section_text(
                sections, group_cfg["sections"], group_name
            )
            if not section_text:
                logger.debug("No section text for group '%s'; skipping", group_name)
                continue

            if group_name == "survey":
                # C3: chunked extraction to handle questionnaires with many variables
                variables = self._extract_survey_variables_chunked(
                    section_text, group_cfg["instructions"]
                )
                if variables:
                    group_results.append({"variables": variables})
            else:
                partial = self._extract_group(
                    section_text=section_text,
                    target_fields=group_cfg["fields"],
                    instructions=group_cfg["instructions"],
                    group_name=group_name,
                )
                if partial:
                    group_results.append(partial)

        merged = self._merge_extractions(group_results)

        # L3: cross-check extracted values against THIS file's source text
        if self.enable_source_check:
            merged = self._check_against_source(merged, text)

        return merged

    # ------------------------------------------------------------------
    # Pass 1 – Section detection
    # ------------------------------------------------------------------

    def _extract_sections(self, text: str) -> Dict[str, str]:
        """Heuristically split *text* into named sections.

        Returns a dict mapping section name → section text.  Always includes
        a ``"full"`` key with the complete text (for groups whose sections
        were not detected).  The ``"preamble"`` key holds text before the
        first recognised heading.
        """
        sections: Dict[str, List[str]] = {"preamble": []}
        current_section = "preamble"

        for line in text.splitlines():
            stripped = line.strip()
            matched_section = self._match_section_heading(stripped)
            if matched_section:
                # Save accumulated lines under the previous section name
                sections.setdefault(current_section, [])
                current_section = matched_section
                sections.setdefault(current_section, [])
            else:
                sections.setdefault(current_section, []).append(line)

        # Convert lists → strings and drop empty sections
        result: Dict[str, str] = {"full": text}
        for name, lines in sections.items():
            content = "\n".join(lines).strip()
            if content:
                result[name] = content

        # B3: If no survey_items heading was detected, auto-promote question blocks
        if self.enable_survey_promotion:
            result = self._promote_survey_items(result)

        return result

    def _match_section_heading(self, line: str) -> Optional[str]:
        """Return the section name if *line* matches a heading pattern.

        Tries two candidates in order:

        1. The original *line* (handles bare headings: "Methods").
        2. The line with a leading numbering prefix removed (handles numbered
           headings: "1. Methods", "第2章 方法", "II. Methods", "3.1 Methods").

        Returns the first matching section name, or *None*.
        """
        if not line:
            return None
        # Ignore very long lines — unlikely to be a section heading
        if len(line) > 120:
            return None

        # Build candidate list: original + prefix-stripped (if different)
        candidates = [line]
        stripped = _NUMBERED_HEADING_PREFIX.sub("", line).strip()
        if stripped and stripped != line:
            candidates.append(stripped)

        for candidate in candidates:
            for section_name, patterns in SECTION_PATTERNS.items():
                for pattern in patterns:
                    if re.fullmatch(pattern, candidate):
                        return section_name
        return None

    def _promote_survey_items(self, sections: Dict[str, str]) -> Dict[str, str]:
        """Auto-detect questionnaire question blocks and promote them to survey_items (B3).

        When :meth:`_extract_sections` did **not** detect a ``survey_items``
        section (because the questionnaire has no explicit heading such as
        "調査票" or "Questionnaire"), this method checks whether the
        ``preamble`` text contains at least :data:`_B3_MIN_QUESTION_COUNT`
        question-number boundaries (:data:`_QUESTION_BOUNDARY_RE`).

        If the threshold is met, the preamble is split at the first detected
        question boundary:

        * Text **before** the first question → stays in ``preamble``
          (used by the *bibliographic* extraction group to extract cover-page
          / introductory metadata).
        * Text **from the first question onwards** → becomes ``survey_items``
          (used by the *survey* extraction group for variable extraction).

        The ``full`` text key is left unchanged.

        Parameters
        ----------
        sections:
            Section dict produced by :meth:`_extract_sections`.

        Returns
        -------
        dict
            Modified section dict with ``survey_items`` added, or the
            original dict unchanged if no promotion is needed.
        """
        if "survey_items" in sections:
            # Explicit heading already detected — nothing to do
            return sections

        preamble_text = sections.get("preamble", "")
        if not preamble_text:
            return sections

        matches = list(_QUESTION_BOUNDARY_RE.finditer(preamble_text))
        if len(matches) < _B3_MIN_QUESTION_COUNT:
            logger.debug(
                "B3: %d question boundary/boundaries in preamble "
                "(threshold %d) — skipping survey_items promotion",
                len(matches),
                _B3_MIN_QUESTION_COUNT,
            )
            return sections

        first_boundary = matches[0].start()
        before = preamble_text[:first_boundary].strip()
        survey_text = preamble_text[first_boundary:].strip()

        if not survey_text:
            return sections

        logger.info(
            "B3: Promoted %d chars as survey_items "
            "(%d question boundaries detected, first at offset %d)",
            len(survey_text),
            len(matches),
            first_boundary,
        )

        new_sections = dict(sections)
        if before:
            new_sections["preamble"] = before
        else:
            new_sections.pop("preamble", None)

        new_sections["survey_items"] = survey_text
        return new_sections

    # ------------------------------------------------------------------
    # Pass 2 – Extraction per group
    # ------------------------------------------------------------------

    def _build_section_text(
        self,
        sections: Dict[str, str],
        section_names: List[str],
        group_name: str,
    ) -> str:
        """Concatenate requested sections within the token budget.

        Falls back to the ``"full"`` section (trimmed) when none of the
        requested named sections were detected.
        """
        budget_chars = _char_budget(self.context_tokens)

        parts: List[str] = []
        for name in section_names:
            if name in sections and name not in _SKIP_SECTIONS:
                parts.append(f"=== {name.upper()} ===\n{sections[name]}")

        if not parts:
            # No named sections found — use full text trimmed to budget
            full_text = sections.get("full", "")
            if full_text:
                logger.debug(
                    "Group '%s': no named sections found; using full text (trimmed)",
                    group_name,
                )
                return self._trim_to_budget(full_text, budget_chars)
            return ""

        combined = "\n\n".join(parts)
        return self._trim_to_budget(combined, budget_chars)

    @staticmethod
    def _trim_to_budget(text: str, budget_chars: int) -> str:
        """Trim *text* to at most *budget_chars* characters."""
        if len(text) <= budget_chars:
            return text
        return text[:budget_chars] + "\n[... truncated for token budget ...]"

    def _extract_group(
        self,
        section_text: str,
        target_fields: List[str],
        instructions: str,
        group_name: str,
    ) -> Dict[str, Any]:
        """Call the LLM to extract *target_fields* from *section_text*.

        RAG examples (when available) are prepended to the prompt as
        YAML-formatted demonstration outputs.
        """
        rag_blocks = self._build_rag_examples(section_text, target_fields)

        prompt_parts: List[str] = []
        prompt_parts.append(
            f"You are a social-science metadata expert.\n"
            f"\n"
            f"{_HALLUCINATION_GUARD}\n"
            f"{instructions}\n"
            f"Return ONLY valid YAML — no markdown fences, no prose.\n"
            f"Fields to extract: {', '.join(target_fields)}\n"
        )

        if rag_blocks:
            prompt_parts.append("--- Example outputs ---")
            prompt_parts.extend(rag_blocks)
            prompt_parts.append("--- End examples ---\n")

        prompt_parts.append("Text to analyse:")
        prompt_parts.append(section_text)
        prompt_parts.append("\nYAML output:")

        prompt = "\n".join(prompt_parts)
        logger.info("Calling LLM for group '%s' (%d chars prompt)", group_name, len(prompt))

        try:
            response = self.extractor.model.prompt(prompt)
            raw_text = response.text()
        except Exception as exc:
            logger.error("LLM call failed for group '%s': %s", group_name, exc)
            return {}

        return self._parse_yaml_response(raw_text, target_fields)

    def _build_rag_examples(
        self, query_text: str, target_fields: List[str]
    ) -> List[str]:
        """Retrieve existing DDI records from the knowledge source and format
        them as YAML example blocks.

        Returns an empty list when no knowledge source is configured or when
        the collection is empty.
        """
        if self.knowledge_source is None:
            return []

        try:
            results = list(
                self.knowledge_source.search(
                    query_text,
                    collection=self.knowledge_source_collection,
                    limit=self.rag_example_limit,
                )
            )
        except Exception as exc:
            logger.debug("RAG search failed: %s", exc)
            return []

        blocks: List[str] = []
        for obj, _score, _meta in results:
            relevant = {
                k: v
                for k, v in obj.items()
                if k in target_fields and v not in (None, "", [], {})
            }
            if relevant:
                blocks.append(yaml.dump(relevant, allow_unicode=True, sort_keys=False))

        return blocks

    # ------------------------------------------------------------------
    # Merging
    # ------------------------------------------------------------------

    def _merge_extractions(self, extractions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Merge partial extraction dicts into a single DDI metadata dict.

        List-valued fields are de-duplicated and concatenated.
        Scalar fields use the first non-None value (earlier groups win).
        """
        merged: Dict[str, Any] = {}
        for partial in extractions:
            if not isinstance(partial, dict):
                continue
            for key, value in partial.items():
                if value is None:
                    continue
                if key not in merged:
                    merged[key] = value
                elif isinstance(merged[key], list) and isinstance(value, list):
                    # Append new items without duplicates (by repr)
                    existing_reprs = {repr(item) for item in merged[key]}
                    for item in value:
                        if repr(item) not in existing_reprs:
                            merged[key].append(item)
                            existing_reprs.add(repr(item))
                # Scalar: keep first value
        return merged

    # ------------------------------------------------------------------
    # Hallucination source-check (C1)
    # ------------------------------------------------------------------

    def _check_against_source(
        self,
        extracted: Dict[str, Any],
        source_text: str,
    ) -> Dict[str, Any]:
        """Cross-check extracted field values against the original source text.

        For each field in :data:`_SOURCE_CHECK_FIELDS`:

        * **Scalar strings** — if the value is not found as a substring of
          *source_text* (case-insensitive, whitespace-normalised), it is
          logged at WARNING level and set to ``None``.
        * **Lists of strings** — items not found in *source_text* are logged
          and removed; if the filtered list is empty the field is set to
          ``None``.
        * **Lists of dicts** (e.g. ``time_periods``) and all other field types
          are left unchanged.

        The check uses case-insensitive substring matching with whitespace
        normalisation, so minor spacing differences between the extracted
        value and the source are tolerated.

        Parameters
        ----------
        extracted:
            The merged DDI metadata dict produced by :meth:`_merge_extractions`.
        source_text:
            The original full document text passed to
            :meth:`extract_ddi_from_text`.

        Returns
        -------
        dict
            A copy of *extracted* with suspicious values removed/nulled.
        """
        # Pre-process source once for efficiency
        source_norm = re.sub(r"\s+", " ", source_text).lower()

        def _found(value: str) -> bool:
            """Return True if *value* appears (normalised) in *source_norm*."""
            needle = re.sub(r"\s+", " ", value.strip()).lower()
            return bool(needle) and needle in source_norm

        cleaned: Dict[str, Any] = dict(extracted)

        for field in _SOURCE_CHECK_FIELDS:
            val = cleaned.get(field)
            if val is None:
                continue

            if isinstance(val, str):
                if not _found(val):
                    logger.warning(
                        "Hallucination guard [%s]: %r not found in source text — "
                        "setting to null. Review and restore manually if incorrect.",
                        field,
                        val[:120],
                    )
                    cleaned[field] = None

            elif isinstance(val, list):
                kept: List[Any] = []
                for item in val:
                    if isinstance(item, str):
                        if _found(item):
                            kept.append(item)
                        else:
                            logger.warning(
                                "Hallucination guard [%s]: item %r not found in "
                                "source text — dropping. Restore manually if incorrect.",
                                field,
                                item[:120],
                            )
                    else:
                        # dict items (e.g. time_periods) — skip check
                        kept.append(item)
                cleaned[field] = kept if kept else None

        return cleaned

    # ------------------------------------------------------------------
    # C3 – Chunked survey-variable extraction
    # ------------------------------------------------------------------

    def _split_survey_blocks(self, text: str) -> List[str]:
        """Split survey text at question boundaries into blocks of :attr:`vars_per_block` questions.

        Uses :data:`_QUESTION_BOUNDARY_RE` to detect the start position of
        each survey variable / question entry.  The text is then partitioned
        so that each block contains at most :attr:`vars_per_block` question
        entries.

        Any preamble text before the first detected question boundary is
        included at the beginning of the first block.

        Parameters
        ----------
        text:
            Survey section text (plain or pipe-delimited A2 format).

        Returns
        -------
        list[str]
            One or more non-empty text blocks.  Returns ``[text]`` when the
            total number of detected boundaries does not exceed
            :attr:`vars_per_block` (i.e. chunking is not needed).
        """
        matches = list(_QUESTION_BOUNDARY_RE.finditer(text))

        if len(matches) <= self.vars_per_block:
            return [text]

        # Build split-point list: each split point is the char-offset of the
        # first question in the next block.
        positions = [m.start() for m in matches]
        # Block i covers positions[i*N] … positions[(i+1)*N - 1].
        # The text slice is text[split_points[i] : split_points[i+1]].
        split_points = [0]
        for i in range(self.vars_per_block, len(positions), self.vars_per_block):
            split_points.append(positions[i])
        split_points.append(len(text))

        blocks: List[str] = []
        for i in range(len(split_points) - 1):
            block = text[split_points[i] : split_points[i + 1]]
            if block.strip():
                blocks.append(block.strip())

        return blocks

    def _extract_survey_variables_chunked(
        self,
        section_text: str,
        instructions: str,
    ) -> List[Dict[str, Any]]:
        """Extract survey variables using block-level chunked LLM calls (C3).

        When :meth:`_split_survey_blocks` returns more than one block, the
        LLM is called once per block and the ``variables`` lists are
        concatenated into a single flat list.

        Parameters
        ----------
        section_text:
            The survey section text (may be pipe-delimited A2 format).
        instructions:
            Extraction instructions from :data:`EXTRACTION_GROUPS`.

        Returns
        -------
        list
            Flat list of variable dicts, combined from all blocks.
        """
        blocks = self._split_survey_blocks(section_text)

        if len(blocks) == 1:
            # No chunking needed — use the standard extraction path
            result = self._extract_group(
                section_text=section_text,
                target_fields=["variables"],
                instructions=instructions,
                group_name="survey",
            )
            return result.get("variables") or []

        logger.info(
            "C3: survey text split into %d block(s) (%d vars/block limit)",
            len(blocks),
            self.vars_per_block,
        )

        all_variables: List[Dict[str, Any]] = []
        for idx, block in enumerate(blocks, start=1):
            logger.info(
                "C3: extracting variables block %d/%d (%d chars)",
                idx,
                len(blocks),
                len(block),
            )
            result = self._extract_group(
                section_text=block,
                target_fields=["variables"],
                instructions=instructions,
                group_name=f"survey_block_{idx}",
            )
            block_vars = result.get("variables")
            if isinstance(block_vars, list):
                all_variables.extend(block_vars)

        logger.info(
            "C3: %d variable(s) extracted in total from %d block(s)",
            len(all_variables),
            len(blocks),
        )
        return all_variables

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _read_full_document(self, file_path: str) -> str:
        """Read *file_path* in full, using pdfplumber → textract fallback.

        When *enable_table_extraction* is True (default) and the file is a
        PDF, each page is processed by :meth:`_extract_pdf_page_text` which
        detects tables using pdfplumber's ``find_tables()`` API and formats
        them as pipe-delimited text to preserve column order.
        """
        suffix = Path(file_path).suffix.lower()
        plain_exts = {".py", ".md", ".txt", ".csv", ".json", ".yaml", ".yml"}

        if suffix in plain_exts:
            return Path(file_path).read_text(encoding="utf-8", errors="replace")

        if suffix == ".pdf":
            try:
                import pdfplumber

                pages = []
                with pdfplumber.open(file_path) as pdf:
                    for page in pdf.pages:
                        if self.enable_table_extraction:
                            page_text = self._extract_pdf_page_text(page)
                        else:
                            page_text = page.extract_text() or ""
                        if page_text.strip():
                            pages.append(page_text)
                text = "\n\n".join(pages)
                if text.strip():
                    return text
                logger.debug("pdfplumber returned empty text for %s", file_path)
            except ImportError:
                logger.debug("pdfplumber not installed; using textract for %s", file_path)
            except Exception as exc:
                logger.debug("pdfplumber failed for %s: %s; trying textract", file_path, exc)

        import textract

        raw = textract.process(file_path)
        return raw.decode("utf-8", errors="replace")

    def _extract_pdf_page_text(self, page: Any) -> str:
        """Extract text from a single pdfplumber *page* object.

        **Table-aware extraction (A2)**:

        1. Call ``page.find_tables()`` to locate all table regions.
        2. For each table (sorted top-to-bottom):

           * Extract the **non-table strip** above it via ``page.crop()``.
           * Format the table itself as pipe-delimited rows via
             :meth:`_table_to_text`.

        3. Extract the remaining strip below the last table.

        This preserves the column → row structure of survey questionnaire
        tables (question number | question text | response categories) which
        ``extract_text()`` alone scrambles.

        Falls back to ``page.extract_text()`` when:

        * ``find_tables()`` raises an exception.
        * No tables are detected on the page.
        * A crop/extract operation fails for an individual region.
        """
        try:
            tables = page.find_tables()
        except Exception as exc:
            logger.debug("find_tables() failed: %s — falling back to extract_text()", exc)
            return page.extract_text() or ""

        if not tables:
            return page.extract_text() or ""

        logger.debug(
            "Page %s: %d table(s) detected — using table-aware extraction",
            getattr(page, "page_number", "?"),
            len(tables),
        )

        page_width: float = page.width
        page_height: float = page.height

        # Sort tables top-to-bottom by their upper edge
        sorted_tables = sorted(tables, key=lambda t: t.bbox[1])

        parts: List[str] = []
        prev_bottom: float = 0.0

        for table in sorted_tables:
            x0, top, x1, bottom = table.bbox

            # ── Strip above this table ──────────────────────────────────
            if top > prev_bottom + 1:  # 1-pt tolerance for floating-point
                try:
                    above_crop = page.crop((0, prev_bottom, page_width, top))
                    above_text = above_crop.extract_text()
                    if above_text and above_text.strip():
                        parts.append(above_text.strip())
                except Exception as exc:
                    logger.debug("crop above table failed: %s", exc)

            # ── Table itself ────────────────────────────────────────────
            try:
                rows = table.extract()
                table_text = self._table_to_text(rows) if rows else ""
                if table_text:
                    parts.append(table_text)
            except Exception as exc:
                logger.debug("table.extract() failed: %s — skipping table", exc)

            prev_bottom = bottom

        # ── Strip below the last table ──────────────────────────────────
        if prev_bottom < page_height - 1:
            try:
                below_crop = page.crop((0, prev_bottom, page_width, page_height))
                below_text = below_crop.extract_text()
                if below_text and below_text.strip():
                    parts.append(below_text.strip())
            except Exception as exc:
                logger.debug("crop below table failed: %s", exc)

        combined = "\n\n".join(parts)

        # Safety: if the table-aware extraction produced nothing, fall back
        if not combined.strip():
            logger.debug("Table-aware extraction yielded empty text; falling back to extract_text()")
            return page.extract_text() or ""

        return combined

    @staticmethod
    def _table_to_text(rows: List[List[Optional[str]]]) -> str:
        """Convert a pdfplumber table (list of rows) to pipe-delimited text.

        Each row becomes a ``| cell1 | cell2 | … |`` line.  Cells that are
        ``None`` (merged/empty) are rendered as empty strings.  Rows where
        every cell is empty are skipped.

        Parameters
        ----------
        rows:
            Nested list as returned by ``pdfplumber.Table.extract()``.

        Returns
        -------
        str
            Pipe-delimited representation, one row per line.
        """
        lines: List[str] = []
        for row in rows:
            if row is None:
                continue
            cells = [str(c).strip() if c is not None else "" for c in row]
            # Skip rows where every cell is empty (blank spacer rows)
            if not any(cells):
                continue
            lines.append("| " + " | ".join(cells) + " |")
        return "\n".join(lines)

    @staticmethod
    def _parse_yaml_response(
        text: str, expected_fields: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Parse a YAML string returned by the LLM.

        Strips markdown code fences if present.  Returns an empty dict on
        parse failure.
        """
        # Strip ```yaml ... ``` or ``` ... ``` fences
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text.strip(), flags=re.MULTILINE)
        text = re.sub(r"\n?```$", "", text.strip(), flags=re.MULTILINE)
        text = text.strip()

        try:
            obj = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            logger.warning("Could not parse YAML response: %s\nText: %s", exc, text[:200])
            return {}

        if not isinstance(obj, dict):
            logger.warning("YAML response is not a dict: %r", type(obj))
            return {}

        # Filter to expected fields only (avoids hallucinated keys)
        if expected_fields:
            obj = {k: v for k, v in obj.items() if k in expected_fields}

        return obj
