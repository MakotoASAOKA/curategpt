"""Tests for DDIXMLSerializer.

These tests use only the standard library (no network, no LLM, no API key).
Run with:  pytest tests/wrappers/test_ddi_xml_serializer.py -v
"""

import xml.etree.ElementTree as ET

import pytest

from curategpt.wrappers.social.ddi_xml_serializer import (
    DDI_NS,
    DDIXMLSerializer,
    _normalise_format,
    _sanitise_id,
    to_ddi_xml,
)

# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

DDI_MINIMAL = {
    "id": "test-001",
    "title": "テスト社会調査 2024",
    "abstract": "本調査は日本全国の成人を対象とした意識調査である。",
    "principal_investigators": ["山田太郎", "鈴木花子"],
}

DDI_FULL = {
    "id": "oai:gesis.org:ZA2800",
    "title": "ALLBUS 1980",
    "alternative_title": "German General Social Survey 1980",
    "study_ids": ["ZA2800", "ICPSR-1234"],
    "principal_investigators": ["Wilhelm Bürklin", "Hans-Dieter Klingemann"],
    "funding_agencies": ["Deutsche Forschungsgemeinschaft"],
    "distributors": ["GESIS – Leibniz Institute for the Social Sciences"],
    "abstract": (
        "A representative survey of the German population covering political "
        "attitudes, social inequality and life satisfaction."
    ),
    "keywords": ["political attitudes", "social inequality", "Germany"],
    "topic_classification": ["Political Behavior and Attitudes"],
    "time_periods": [
        {"event": "start", "date": "1980-03", "label": "March 1980"},
        {"event": "end", "date": "1980-06", "label": "June 1980"},
    ],
    "collection_dates": [
        {"event": "start", "date": "1980-03-01"},
        {"event": "end", "date": "1980-06-30"},
    ],
    "nations": ["Germany (West)"],
    "geographic_coverage": ["Federal Republic of Germany"],
    "universe": "German-speaking persons 18 years or older living in private households.",
    "analysis_unit": "Individual",
    "sampling_procedure": "Stratified random sample",
    "collection_mode": "Face-to-face interview",
    "time_method": "Cross-section",
    "collection_situation": "Home interview",
    "weighting": "Design weight applied.",
    "cleaning_operations": "Range checks performed.",
    "access_place": "GESIS Data Archive",
    "access_conditions": "Available for academic research.",
    "special_permissions": "None required.",
    "file_name": "ZA2800_v1.dta",
    "file_type": "Stata",
    "file_content_description": "Data file for ALLBUS 1980",
    "variables": [
        {
            "name": "SEX",
            "label": "性別",
            "question": "あなたの性別を教えてください。",
            "categories": [
                {"value": "1", "label": "男性"},
                {"value": "2", "label": "女性"},
            ],
            "format": "numeric",
        },
        {
            "name": "AGE",
            "label": "年齢",
            "question": "あなたの年齢を教えてください。",
            "categories": [],
            "format": "integer",
        },
        {
            "name": "COMMENT",
            "label": "自由回答",
            "question": None,
            "categories": None,
            "format": "string",
        },
    ],
}


# ---------------------------------------------------------------------------
# Helper: parse XML string → ElementTree with namespace
# ---------------------------------------------------------------------------

def _parse(xml_str: str) -> ET.Element:
    return ET.fromstring(xml_str)


def _find(root: ET.Element, path: str) -> ET.Element:
    """Find using Clark-notation path segments separated by '/'."""
    parts = path.split("/")
    el = root
    for part in parts:
        el = el.find(f"{{{DDI_NS}}}{part}")
        if el is None:
            return None
    return el


def _findall(root: ET.Element, path: str):
    """Findall using Clark-notation path segments."""
    parts = path.split("/")
    el = root
    for part in parts[:-1]:
        el = el.find(f"{{{DDI_NS}}}{part}")
        if el is None:
            return []
    return el.findall(f"{{{DDI_NS}}}{parts[-1]}")


# ---------------------------------------------------------------------------
# Tests: serialiser output
# ---------------------------------------------------------------------------

class TestDDIXMLSerializerMinimal:
    """Tests with a minimal DDI dict."""

    def setup_method(self):
        self.s = DDIXMLSerializer()
        self.xml_str = self.s.to_xml_string(DDI_MINIMAL)
        self.root = _parse(self.xml_str)

    def test_xml_declaration_present(self):
        assert self.xml_str.startswith("<?xml")

    def test_root_tag_is_codebook(self):
        assert self.root.tag == f"{{{DDI_NS}}}codeBook"

    def test_version_attribute(self):
        assert self.root.get("version") == "2.5"

    def test_id_attribute(self):
        assert self.root.get("ID") == "test-001"

    def test_title_present(self):
        titl = _find(self.root, "stdyDscr/citation/titlStmt/titl")
        assert titl is not None
        assert titl.text == "テスト社会調査 2024"

    def test_investigators_present(self):
        auth_entities = _findall(
            self.root, "stdyDscr/citation/rspStmt/AuthEnty"
        )
        names = [el.text for el in auth_entities]
        assert "山田太郎" in names
        assert "鈴木花子" in names

    def test_abstract_present(self):
        abstract = _find(self.root, "stdyDscr/stdyInfo/abstract")
        assert abstract is not None
        assert "意識調査" in abstract.text

    def test_no_file_dscr_when_absent(self):
        file_dscr = _find(self.root, "fileDscr")
        assert file_dscr is None

    def test_no_data_dscr_when_absent(self):
        data_dscr = _find(self.root, "dataDscr")
        assert data_dscr is None


class TestDDIXMLSerializerFull:
    """Tests with a complete DDI dict."""

    def setup_method(self):
        self.s = DDIXMLSerializer()
        self.xml_str = self.s.to_xml_string(DDI_FULL)
        self.root = _parse(self.xml_str)

    # --- Citation ----------------------------------------------------------

    def test_alternative_title(self):
        alt = _find(self.root, "stdyDscr/citation/titlStmt/altTitl")
        assert alt is not None
        assert "German General Social Survey" in alt.text

    def test_study_ids(self):
        ids = _findall(self.root, "stdyDscr/citation/titlStmt/IDNo")
        id_texts = [el.text for el in ids]
        assert "ZA2800" in id_texts
        assert "ICPSR-1234" in id_texts

    def test_funding_agency(self):
        fund = _find(self.root, "stdyDscr/citation/prodStmt/fundAg")
        assert fund is not None
        assert "Forschungsgemeinschaft" in fund.text

    def test_distributor(self):
        distr = _find(self.root, "stdyDscr/citation/distStmt/distrbtr")
        assert distr is not None
        assert "GESIS" in distr.text

    # --- stdyInfo ----------------------------------------------------------

    def test_keywords(self):
        kws = _findall(self.root, "stdyDscr/stdyInfo/subject/keyword")
        kw_texts = [el.text for el in kws]
        assert "political attitudes" in kw_texts
        assert "Germany" in kw_texts

    def test_topic_classification(self):
        topics = _findall(
            self.root, "stdyDscr/stdyInfo/subject/topcClas"
        )
        assert len(topics) == 1
        assert "Political Behavior" in topics[0].text

    def test_time_periods(self):
        time_prds = _findall(self.root, "stdyDscr/stdyInfo/sumDscr/timePrd")
        assert len(time_prds) == 2
        events = [el.get("event") for el in time_prds]
        assert "start" in events
        assert "end" in events
        dates = [el.get("date") for el in time_prds]
        assert "1980-03" in dates

    def test_collection_dates(self):
        coll_dates = _findall(
            self.root, "stdyDscr/stdyInfo/sumDscr/collDate"
        )
        assert len(coll_dates) == 2

    def test_nation(self):
        nations = _findall(self.root, "stdyDscr/stdyInfo/sumDscr/nation")
        assert len(nations) == 1
        assert "Germany" in nations[0].text

    def test_universe(self):
        universe = _find(self.root, "stdyDscr/stdyInfo/sumDscr/universe")
        assert universe is not None
        assert "18 years" in universe.text

    def test_analysis_unit(self):
        unit = _find(self.root, "stdyDscr/stdyInfo/sumDscr/anlyUnit")
        assert unit is not None
        assert unit.text == "Individual"

    # --- method ------------------------------------------------------------

    def test_sampling_procedure(self):
        samp = _find(self.root, "stdyDscr/method/dataColl/sampProc")
        assert samp is not None
        assert "random" in samp.text.lower()

    def test_collection_mode(self):
        mode = _find(self.root, "stdyDscr/method/dataColl/collMode")
        assert mode is not None
        assert "interview" in mode.text.lower()

    # --- dataAccs ----------------------------------------------------------

    def test_access_place(self):
        place = _find(
            self.root, "stdyDscr/dataAccs/setAvail/accsPlac"
        )
        assert place is not None
        assert "GESIS" in place.text

    def test_access_conditions(self):
        cond = _find(
            self.root, "stdyDscr/dataAccs/useStmt/restrctn"
        )
        assert cond is not None

    # --- fileDscr ----------------------------------------------------------

    def test_file_name(self):
        fname = _find(self.root, "fileDscr/fileTxt/fileName")
        assert fname is not None
        assert fname.text == "ZA2800_v1.dta"

    def test_file_type(self):
        ftype = _find(self.root, "fileDscr/fileTxt/fileType")
        assert ftype is not None
        assert ftype.text == "Stata"

    # --- dataDscr / variables ----------------------------------------------

    def test_variable_count(self):
        vars_ = _findall(self.root, "dataDscr/var")
        assert len(vars_) == 3

    def test_variable_id_and_name(self):
        vars_ = _findall(self.root, "dataDscr/var")
        first = vars_[0]
        assert first.get("ID") == "V1"
        assert first.get("name") == "SEX"

    def test_variable_label(self):
        vars_ = _findall(self.root, "dataDscr/var")
        label = vars_[0].find(f"{{{DDI_NS}}}labl")
        assert label is not None
        assert label.text == "性別"

    def test_variable_question(self):
        vars_ = _findall(self.root, "dataDscr/var")
        qstn_lit = vars_[0].find(
            f"{{{DDI_NS}}}qstn/{{{DDI_NS}}}qstnLit"
        )
        assert qstn_lit is not None
        assert "性別" in qstn_lit.text

    def test_variable_categories(self):
        vars_ = _findall(self.root, "dataDscr/var")
        categories = vars_[0].findall(f"{{{DDI_NS}}}catgry")
        assert len(categories) == 2
        first_val = categories[0].find(f"{{{DDI_NS}}}catValu")
        assert first_val.text == "1"
        first_label = categories[0].find(f"{{{DDI_NS}}}labl")
        assert first_label.text == "男性"

    def test_variable_format_numeric(self):
        vars_ = _findall(self.root, "dataDscr/var")
        fmt = vars_[0].find(f"{{{DDI_NS}}}varFormat")
        assert fmt is not None
        assert fmt.get("type") == "numeric"

    def test_variable_format_integer_normalised(self):
        """'integer' in source dict → 'numeric' in DDI XML."""
        vars_ = _findall(self.root, "dataDscr/var")
        fmt = vars_[1].find(f"{{{DDI_NS}}}varFormat")
        assert fmt.get("type") == "numeric"

    def test_variable_format_string_normalised(self):
        """'string' in source dict → 'character' in DDI XML."""
        vars_ = _findall(self.root, "dataDscr/var")
        fmt = vars_[2].find(f"{{{DDI_NS}}}varFormat")
        assert fmt.get("type") == "character"

    def test_variable_no_question_when_null(self):
        """No <qstn> element when question is None."""
        vars_ = _findall(self.root, "dataDscr/var")
        qstn = vars_[2].find(f"{{{DDI_NS}}}qstn")
        assert qstn is None

    def test_doc_dscr_has_prod_date(self):
        prod_date = _find(self.root, "docDscr/citation/prodStmt/prodDate")
        assert prod_date is not None
        # Should be an ISO date string (YYYY-MM-DD)
        import re
        assert re.match(r"\d{4}-\d{2}-\d{2}", prod_date.text)


# ---------------------------------------------------------------------------
# Tests: to_xml() → bytes
# ---------------------------------------------------------------------------

def test_to_xml_returns_bytes():
    s = DDIXMLSerializer()
    result = s.to_xml(DDI_MINIMAL)
    assert isinstance(result, bytes)
    assert result.startswith(b"<?xml")


def test_to_xml_string_returns_str():
    s = DDIXMLSerializer()
    result = s.to_xml_string(DDI_MINIMAL)
    assert isinstance(result, str)


def test_save_creates_file(tmp_path):
    s = DDIXMLSerializer()
    out = tmp_path / "output.xml"
    s.save(DDI_MINIMAL, out)
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "テスト社会調査" in content


def test_save_creates_parent_dirs(tmp_path):
    s = DDIXMLSerializer()
    out = tmp_path / "nested" / "dir" / "output.xml"
    s.save(DDI_MINIMAL, out)
    assert out.exists()


def test_convenience_function_returns_string():
    xml_str = to_ddi_xml(DDI_MINIMAL)
    assert isinstance(xml_str, str)
    assert "テスト社会調査" in xml_str


def test_convenience_function_writes_file(tmp_path):
    out = tmp_path / "conv.xml"
    to_ddi_xml(DDI_MINIMAL, file_path=out)
    assert out.exists()


# ---------------------------------------------------------------------------
# Tests: validate_structure()
# ---------------------------------------------------------------------------

def test_validate_minimal_has_warnings():
    s = DDIXMLSerializer()
    ok, errors = s.validate_structure(DDI_MINIMAL)
    # title is present, but abstract and principal_investigators will trigger
    # recommended warnings — but DDI_MINIMAL has abstract and investigators
    # so it should pass
    assert ok is True
    assert errors == []


def test_validate_missing_title():
    s = DDIXMLSerializer()
    d = {"abstract": "Some abstract"}
    ok, errors = s.validate_structure(d)
    assert ok is False
    assert any("title" in e for e in errors)


def test_validate_wrong_type_for_list_field():
    s = DDIXMLSerializer()
    d = {**DDI_MINIMAL, "keywords": "not-a-list-but-string"}
    # string is acceptable (will be treated as scalar)
    ok, errors = s.validate_structure(d)
    # keywords as str should NOT raise a type warning (str is allowed)
    assert all("keywords" not in e for e in errors)


def test_validate_variables_non_dict_entry():
    s = DDIXMLSerializer()
    d = {**DDI_MINIMAL, "variables": ["invalid-string-entry"]}
    ok, errors = s.validate_structure(d)
    assert ok is False
    assert any("variables" in e for e in errors)


# ---------------------------------------------------------------------------
# Tests: helper utilities
# ---------------------------------------------------------------------------

def test_normalise_format_numeric():
    assert _normalise_format("numeric") == "numeric"
    assert _normalise_format("integer") == "numeric"
    assert _normalise_format("float") == "numeric"
    assert _normalise_format("NUMBER") == "numeric"


def test_normalise_format_character():
    assert _normalise_format("string") == "character"
    assert _normalise_format("text") == "character"
    assert _normalise_format("CHARACTER") == "character"


def test_normalise_format_date():
    assert _normalise_format("date") == "date"
    assert _normalise_format("datetime") == "date"


def test_normalise_format_unknown_passthrough():
    assert _normalise_format("binary") == "binary"


def test_sanitise_id_plain():
    assert _sanitise_id("ZA2800") == "ZA2800"


def test_sanitise_id_spaces_replaced():
    result = _sanitise_id("my study 001")
    assert " " not in result


def test_sanitise_id_colons_replaced():
    result = _sanitise_id("oai:gesis.org:ZA2800")
    assert ":" not in result


def test_sanitise_id_starts_with_digit():
    result = _sanitise_id("123abc")
    assert not result[0].isdigit()


def test_sanitise_id_empty():
    result = _sanitise_id("")
    assert result == "ddi-unknown"


# ---------------------------------------------------------------------------
# Tests: XML is well-formed and round-trippable
# ---------------------------------------------------------------------------

def test_output_is_valid_xml():
    """ET.fromstring should not raise for the generated XML."""
    s = DDIXMLSerializer()
    xml_bytes = s.to_xml(DDI_FULL)
    # Remove XML declaration for fromstring (it doesn't accept it)
    xml_body = xml_bytes.split(b"\n", 1)[1]
    root = ET.fromstring(xml_body)
    assert root is not None


def test_version_override():
    s = DDIXMLSerializer(ddi_version="2.1")
    root = _parse(s.to_xml_string(DDI_MINIMAL))
    assert root.get("version") == "2.1"
