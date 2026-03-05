"""DDI-Codebook 2.5 XML serializer.

Converts a flat DDI metadata dict (as produced by
:class:`~curategpt.wrappers.social.ddi_codebook_wrapper.DDICodebookWrapper`
or :class:`~curategpt.agents.paper_to_ddi_agent.PaperToDDIAgent`) to valid
DDI-Codebook 2.5 XML using the standard-library ``xml.etree.ElementTree``.

No extra dependencies are required beyond the Python standard library.

Field mapping
-------------

dict key                  → DDI XML path
────────────────────────────────────────────────────────────────────────────
id                        → codeBook[@ID]
title                     → stdyDscr/citation/titlStmt/titl
alternative_title         → stdyDscr/citation/titlStmt/altTitl
study_ids                 → stdyDscr/citation/titlStmt/IDNo  (repeatable)
principal_investigators   → stdyDscr/citation/rspStmt/AuthEnty  (repeatable)
funding_agencies          → stdyDscr/citation/prodStmt/fundAg  (repeatable)
distributors              → stdyDscr/citation/distStmt/distrbtr  (repeatable)
abstract                  → stdyDscr/stdyInfo/abstract
keywords                  → stdyDscr/stdyInfo/subject/keyword  (repeatable)
topic_classification      → stdyDscr/stdyInfo/subject/topcClas  (repeatable)
time_periods              → stdyDscr/stdyInfo/sumDscr/timePrd  (repeatable)
collection_dates          → stdyDscr/stdyInfo/sumDscr/collDate  (repeatable)
nations                   → stdyDscr/stdyInfo/sumDscr/nation  (repeatable)
geographic_coverage       → stdyDscr/stdyInfo/sumDscr/geogCover  (repeatable)
universe                  → stdyDscr/stdyInfo/sumDscr/universe
analysis_unit             → stdyDscr/stdyInfo/sumDscr/anlyUnit
sampling_procedure        → stdyDscr/method/dataColl/sampProc
collection_mode           → stdyDscr/method/dataColl/collMode
time_method               → stdyDscr/method/dataColl/timeMeth
collection_situation      → stdyDscr/method/dataColl/collSitu
weighting                 → stdyDscr/method/dataColl/weight
cleaning_operations       → stdyDscr/method/dataColl/cleanOps
access_place              → stdyDscr/dataAccs/setAvail/accsPlac
access_conditions         → stdyDscr/dataAccs/useStmt/restrctn
special_permissions       → stdyDscr/dataAccs/useStmt/specPerm
file_name                 → fileDscr/fileTxt/fileName
file_type                 → fileDscr/fileTxt/fileType
file_content_description  → fileDscr/fileTxt/fileCont
variables                 → dataDscr/var  (list, each with name/label/question/
                              categories/format)

Typical usage
-------------
::

    from curategpt.wrappers.social.ddi_xml_serializer import DDIXMLSerializer

    s = DDIXMLSerializer()
    xml_str = s.to_xml_string(ddi_dict)          # UTF-8 string
    xml_bytes = s.to_xml(ddi_dict)               # UTF-8 bytes
    s.save(ddi_dict, "output.xml")               # write to file

    # round-trip check
    ok, errors = s.validate_structure(ddi_dict)
    if not ok:
        for e in errors:
            print(e)
"""

import logging
import uuid
import xml.etree.ElementTree as ET
from datetime import date as _today_type
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Namespace constants
# ---------------------------------------------------------------------------

DDI_NS = "ddi:codebook:2_5"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
DDI_SCHEMA_LOCATION = (
    "ddi:codebook:2_5 "
    "http://www.ddialliance.org/Specification/DDI-Codebook/2.5/XMLSchema/codebook.xsd"
)

# Register namespaces BEFORE any element is created so that ElementTree uses
# the human-readable prefixes in the serialised output.
ET.register_namespace("", DDI_NS)
ET.register_namespace("xsi", XSI_NS)

# Clark-notation helper: "{ddi:codebook:2_5}localname"
def _q(local: str) -> str:
    return f"{{{DDI_NS}}}{local}"


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _ensure_list(value: Any) -> List:
    """Normalise scalar / list / None to a list, removing None entries."""
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if v is not None]
    return [value]


def _str_or_none(value: Any) -> Optional[str]:
    """Return a non-empty stripped string or None."""
    if value is None:
        return None
    if isinstance(value, list):
        joined = " ; ".join(str(v).strip() for v in value if v)
        return joined or None
    s = str(value).strip()
    return s or None


def _sub(
    parent: ET.Element,
    local_tag: str,
    text: Optional[str] = None,
    **attrib: str,
) -> ET.Element:
    """Create a child element in the DDI namespace with optional text / attributes."""
    el = ET.SubElement(parent, _q(local_tag), **attrib)
    if text is not None:
        el.text = text
    return el


# ---------------------------------------------------------------------------
# Public convenience function
# ---------------------------------------------------------------------------

def to_ddi_xml(
    ddi_dict: Dict[str, Any],
    file_path: Optional[Union[str, Path]] = None,
    version: str = "2.5",
) -> str:
    """Serialise *ddi_dict* to DDI-Codebook XML and optionally write to a file.

    Parameters
    ----------
    ddi_dict:
        Flat DDI metadata dict (output of DDICodebookWrapper / PaperToDDIAgent).
    file_path:
        When provided, the XML is also written to this path.
    version:
        DDI-Codebook version string (default ``"2.5"``).

    Returns
    -------
    str
        UTF-8 XML string with XML declaration.
    """
    s = DDIXMLSerializer(ddi_version=version)
    xml_str = s.to_xml_string(ddi_dict)
    if file_path:
        s.save(ddi_dict, file_path)
    return xml_str


# ---------------------------------------------------------------------------
# Main serialiser class
# ---------------------------------------------------------------------------

class DDIXMLSerializer:
    """Serialise a DDI metadata dict to DDI-Codebook 2.5 XML.

    The serialiser is stateless except for *ddi_version*.  A single instance
    can be reused for multiple dicts.

    Parameters
    ----------
    ddi_version:
        Value of the ``version`` attribute on ``<codeBook>``.  Defaults to
        ``"2.5"``; pass ``"2.1"`` for older DDI Codebook 2.1 compatibility.
    """

    def __init__(self, ddi_version: str = "2.5") -> None:
        self.ddi_version = ddi_version

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def to_xml(self, ddi_dict: Dict[str, Any]) -> bytes:
        """Return DDI-Codebook XML as UTF-8 encoded bytes (with XML declaration)."""
        root = self._build_tree(ddi_dict)
        ET.indent(root, space="  ")  # requires Python ≥ 3.9
        return ET.tostring(root, encoding="UTF-8", xml_declaration=True)

    def to_xml_string(self, ddi_dict: Dict[str, Any]) -> str:
        """Return DDI-Codebook XML as a UTF-8 string (with XML declaration)."""
        return self.to_xml(ddi_dict).decode("utf-8")

    def save(
        self,
        ddi_dict: Dict[str, Any],
        file_path: Union[str, Path],
    ) -> None:
        """Write DDI-Codebook XML to *file_path*, creating parent dirs as needed."""
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        xml_bytes = self.to_xml(ddi_dict)
        path.write_bytes(xml_bytes)
        logger.info("DDI XML written to %s (%d bytes)", path, len(xml_bytes))

    def validate_structure(
        self, ddi_dict: Dict[str, Any]
    ) -> Tuple[bool, List[str]]:
        """Run lightweight structural checks on *ddi_dict* before serialisation.

        Checks that required / strongly recommended fields are present and
        that list-typed fields contain the expected types.

        Returns
        -------
        (ok, errors)
            *ok* is True when no errors are found.  *errors* is a list of
            human-readable error messages (empty when *ok* is True).
        """
        errors: List[str] = []

        # Required fields
        if not _str_or_none(ddi_dict.get("title")):
            errors.append("MISSING required field: 'title'")

        # Recommended fields
        for field in ("abstract", "principal_investigators", "universe"):
            if not ddi_dict.get(field):
                errors.append(f"MISSING recommended field: '{field}'")

        # List-type fields
        list_fields = (
            "study_ids",
            "principal_investigators",
            "funding_agencies",
            "distributors",
            "keywords",
            "topic_classification",
            "time_periods",
            "collection_dates",
            "nations",
            "geographic_coverage",
            "variables",
        )
        for fname in list_fields:
            val = ddi_dict.get(fname)
            if val is not None and not isinstance(val, (list, str)):
                errors.append(
                    f"TYPE WARNING: '{fname}' should be a list, got {type(val).__name__}"
                )

        # Variables structure check
        for i, var in enumerate(_ensure_list(ddi_dict.get("variables")), start=1):
            if not isinstance(var, dict):
                errors.append(
                    f"INVALID variables[{i}]: expected dict, got {type(var).__name__}"
                )
                continue
            if not var.get("name") and not var.get("label"):
                errors.append(
                    f"WARNING variables[{i}]: neither 'name' nor 'label' provided"
                )

        return (len(errors) == 0), errors

    # ------------------------------------------------------------------
    # Tree builder – top level
    # ------------------------------------------------------------------

    def _build_tree(self, d: Dict[str, Any]) -> ET.Element:
        """Build and return the full ``<codeBook>`` element tree."""
        root = ET.Element(_q("codeBook"))

        # Namespace declarations are emitted automatically by register_namespace;
        # we only need to set the xsi:schemaLocation and DDI attributes.
        root.set(f"{{{XSI_NS}}}schemaLocation", DDI_SCHEMA_LOCATION)
        root.set("version", self.ddi_version)

        # Use existing ID or generate a stable UUID-based one
        record_id = _str_or_none(d.get("id")) or f"ddi-{uuid.uuid4().hex[:8]}"
        # Sanitise: DDI IDs must be XML NCName-compatible (no spaces, colons→hyphens)
        root.set("ID", _sanitise_id(record_id))

        self._build_doc_dscr(root, d)
        self._build_stdy_dscr(root, d)
        self._build_file_dscr(root, d)
        self._build_data_dscr(root, d)
        return root

    # ------------------------------------------------------------------
    # docDscr — document description
    # ------------------------------------------------------------------

    def _build_doc_dscr(self, root: ET.Element, d: Dict[str, Any]) -> None:
        """Build ``<docDscr>`` with a minimal document-level citation."""
        doc_dscr = _sub(root, "docDscr")
        citation = _sub(doc_dscr, "citation")
        titl_stmt = _sub(citation, "titlStmt")

        title = _str_or_none(d.get("title"))
        doc_title = f"Codebook for: {title}" if title else "DDI Codebook"
        _sub(titl_stmt, "titl", text=doc_title)

        prod_stmt = _sub(citation, "prodStmt")
        _sub(prod_stmt, "prodDate", text=str(_today_type.today()))

    # ------------------------------------------------------------------
    # stdyDscr — study description
    # ------------------------------------------------------------------

    def _build_stdy_dscr(self, root: ET.Element, d: Dict[str, Any]) -> None:
        stdy = _sub(root, "stdyDscr")
        self._build_stdy_citation(stdy, d)
        self._build_stdy_info(stdy, d)
        self._build_method(stdy, d)
        self._build_data_accs(stdy, d)

    # --- citation ---------------------------------------------------------

    def _build_stdy_citation(self, stdy: ET.Element, d: Dict[str, Any]) -> None:
        citation = _sub(stdy, "citation")

        # titlStmt
        titl_stmt = _sub(citation, "titlStmt")

        title = _str_or_none(d.get("title"))
        if title:
            _sub(titl_stmt, "titl", text=title)

        alt_title = _str_or_none(d.get("alternative_title"))
        if alt_title:
            _sub(titl_stmt, "altTitl", text=alt_title)

        for sid in _ensure_list(d.get("study_ids")):
            sid_text = _str_or_none(sid)
            if sid_text:
                _sub(titl_stmt, "IDNo", text=sid_text)

        # rspStmt — responsible parties (principal investigators)
        investigators = [
            _str_or_none(v) for v in _ensure_list(d.get("principal_investigators"))
        ]
        investigators = [v for v in investigators if v]
        if investigators:
            rsp = _sub(citation, "rspStmt")
            for inv in investigators:
                _sub(rsp, "AuthEnty", text=inv)

        # prodStmt — funding agencies
        funders = [
            _str_or_none(v) for v in _ensure_list(d.get("funding_agencies"))
        ]
        funders = [v for v in funders if v]
        if funders:
            prod = _sub(citation, "prodStmt")
            for funder in funders:
                _sub(prod, "fundAg", text=funder)

        # distStmt — distributors
        distributors = [
            _str_or_none(v) for v in _ensure_list(d.get("distributors"))
        ]
        distributors = [v for v in distributors if v]
        if distributors:
            dist = _sub(citation, "distStmt")
            for distr in distributors:
                _sub(dist, "distrbtr", text=distr)

    # --- stdyInfo ---------------------------------------------------------

    def _build_stdy_info(self, stdy: ET.Element, d: Dict[str, Any]) -> None:
        stdy_info = _sub(stdy, "stdyInfo")

        # subject — keywords and topic classifications
        keywords = [_str_or_none(v) for v in _ensure_list(d.get("keywords"))]
        keywords = [v for v in keywords if v]
        topics = [
            _str_or_none(v) for v in _ensure_list(d.get("topic_classification"))
        ]
        topics = [v for v in topics if v]

        # CV codes for topic_classification (parallel list from apply_cv_mappings)
        topic_cv_codes = d.get("topic_classification_cv") or []
        if not isinstance(topic_cv_codes, list):
            topic_cv_codes = [topic_cv_codes]
        tc_list_id = d.get("topic_classification_cv_list_id")
        tc_list_ver = d.get("topic_classification_cv_list_ver")

        if keywords or topics:
            subject = _sub(stdy_info, "subject")
            for kw in keywords:
                _sub(subject, "keyword", text=kw)
            for i, tc in enumerate(topics):
                cv_code = topic_cv_codes[i] if i < len(topic_cv_codes) else None
                if cv_code and tc_list_id:
                    tc_attrs: Dict[str, str] = {"codeListID": tc_list_id}
                    if tc_list_ver:
                        tc_attrs["codeListVersionID"] = tc_list_ver
                    _sub(subject, "topcClas", text=cv_code, **tc_attrs)
                else:
                    _sub(subject, "topcClas", text=tc)

        # abstract
        abstract = _str_or_none(d.get("abstract"))
        if abstract:
            _sub(stdy_info, "abstract", text=abstract)

        # sumDscr
        self._build_sum_dscr(stdy_info, d)

    def _build_sum_dscr(self, stdy_info: ET.Element, d: Dict[str, Any]) -> None:
        """Build ``<sumDscr>`` with temporal, geographic and universe elements."""
        tp_list = _ensure_list(d.get("time_periods"))
        cd_list = _ensure_list(d.get("collection_dates"))
        nations = [_str_or_none(v) for v in _ensure_list(d.get("nations")) if v]
        geo_covers = [
            _str_or_none(v)
            for v in _ensure_list(d.get("geographic_coverage"))
            if v
        ]
        universe = _str_or_none(d.get("universe"))
        analysis_unit = _str_or_none(d.get("analysis_unit"))

        if not any([tp_list, cd_list, nations, geo_covers, universe, analysis_unit]):
            return

        sum_dscr = _sub(stdy_info, "sumDscr")

        for tp in tp_list:
            self._build_date_element(sum_dscr, "timePrd", tp)

        for cd in cd_list:
            self._build_date_element(sum_dscr, "collDate", cd)

        for nation in nations:
            _sub(sum_dscr, "nation", text=nation)

        for gc in geo_covers:
            _sub(sum_dscr, "geogCover", text=gc)

        if universe:
            _sub(sum_dscr, "universe", text=universe)

        if analysis_unit:
            cv_code = d.get("analysis_unit_cv")
            cv_list_id = d.get("analysis_unit_cv_list_id")
            cv_list_ver = d.get("analysis_unit_cv_list_ver")
            if cv_code and cv_list_id:
                attribs = {"codeListID": cv_list_id}
                if cv_list_ver:
                    attribs["codeListVersionID"] = cv_list_ver
                _sub(sum_dscr, "anlyUnit", text=cv_code, **attribs)
            else:
                _sub(sum_dscr, "anlyUnit", text=analysis_unit)

    def _build_date_element(
        self, parent: ET.Element, tag: str, value: Any
    ) -> None:
        """Build a ``<timePrd>`` or ``<collDate>`` element.

        *value* can be:

        * ``str`` — used as text content, no attributes.
        * ``dict`` — may contain keys ``event`` (``start``/``end``/``single``),
          ``date`` (ISO 8601), and ``label`` (human-readable text).
        """
        if isinstance(value, dict):
            attrib: Dict[str, str] = {}
            if event := _str_or_none(value.get("event")):
                attrib["event"] = event
            if date_val := _str_or_none(value.get("date")):
                attrib["date"] = date_val
            text = _str_or_none(value.get("label")) or _str_or_none(
                value.get("date")
            )
            _sub(parent, tag, text=text, **attrib)
        elif value is not None:
            text = _str_or_none(value)
            if text:
                _sub(parent, tag, text=text)

    # --- method -----------------------------------------------------------

    def _build_method(self, stdy: ET.Element, d: Dict[str, Any]) -> None:
        """Build ``<method><dataColl>`` with methodology elements.

        When a ``{field}_cv`` key is present (added by
        :func:`~curategpt.wrappers.social.ddi_cv_mapper.apply_cv_mappings`),
        the CV code is used as the element text content and DDI Alliance
        ``codeListID`` / ``codeListVersionID`` attributes are added.
        Otherwise the raw field value is written as-is.
        """
        # (field_key, DDI local tag)
        method_fields = [
            ("time_method", "timeMeth"),
            ("sampling_procedure", "sampProc"),
            ("collection_mode", "collMode"),
            ("collection_situation", "collSitu"),
            ("weighting", "weight"),
            ("cleaning_operations", "cleanOps"),
        ]

        # Collect (xml_tag, text, attribs) triples
        entries = []
        for field_key, xml_tag in method_fields:
            raw = _str_or_none(d.get(field_key))
            if not raw:
                continue
            cv_code = d.get(f"{field_key}_cv")
            cv_list_id = d.get(f"{field_key}_cv_list_id")
            cv_list_ver = d.get(f"{field_key}_cv_list_ver")

            if cv_code and cv_list_id:
                # CV-coded element: use the code as content, add attributes
                attribs = {"codeListID": cv_list_id}
                if cv_list_ver:
                    attribs["codeListVersionID"] = cv_list_ver
                entries.append((xml_tag, cv_code, attribs))
            elif cv_code:
                # CV code exists but no codeListID (e.g. sampProc)
                entries.append((xml_tag, cv_code, {}))
            else:
                # No CV match — write original value verbatim
                entries.append((xml_tag, raw, {}))

        if not entries:
            return

        method = _sub(stdy, "method")
        data_coll = _sub(method, "dataColl")
        for xml_tag, text, attribs in entries:
            _sub(data_coll, xml_tag, text=text, **attribs)

    # --- dataAccs ---------------------------------------------------------

    def _build_data_accs(self, stdy: ET.Element, d: Dict[str, Any]) -> None:
        """Build ``<dataAccs>`` with access conditions."""
        access_place = _str_or_none(d.get("access_place"))
        access_conditions = _str_or_none(d.get("access_conditions"))
        special_permissions = _str_or_none(d.get("special_permissions"))

        if not any([access_place, access_conditions, special_permissions]):
            return

        data_accs = _sub(stdy, "dataAccs")

        if access_place:
            set_avail = _sub(data_accs, "setAvail")
            _sub(set_avail, "accsPlac", text=access_place)

        if access_conditions or special_permissions:
            use_stmt = _sub(data_accs, "useStmt")
            if access_conditions:
                _sub(use_stmt, "restrctn", text=access_conditions)
            if special_permissions:
                _sub(use_stmt, "specPerm", text=special_permissions)

    # ------------------------------------------------------------------
    # fileDscr — file description
    # ------------------------------------------------------------------

    def _build_file_dscr(self, root: ET.Element, d: Dict[str, Any]) -> None:
        """Build ``<fileDscr>`` when any file metadata is present."""
        file_name = _str_or_none(d.get("file_name"))
        file_type = _str_or_none(d.get("file_type"))
        file_content = _str_or_none(d.get("file_content_description"))

        if not any([file_name, file_type, file_content]):
            return

        file_dscr = _sub(root, "fileDscr")
        file_txt = _sub(file_dscr, "fileTxt")

        if file_name:
            _sub(file_txt, "fileName", text=file_name)
        if file_type:
            _sub(file_txt, "fileType", text=file_type)
        if file_content:
            _sub(file_txt, "fileCont", text=file_content)

    # ------------------------------------------------------------------
    # dataDscr — variable descriptions
    # ------------------------------------------------------------------

    def _build_data_dscr(self, root: ET.Element, d: Dict[str, Any]) -> None:
        """Build ``<dataDscr>`` from the *variables* list."""
        variables = [v for v in _ensure_list(d.get("variables")) if isinstance(v, dict)]
        if not variables:
            return

        data_dscr = _sub(root, "dataDscr")
        for i, var in enumerate(variables, start=1):
            self._build_var(data_dscr, var, index=i)

    def _build_var(
        self, data_dscr: ET.Element, var: Dict[str, Any], index: int
    ) -> None:
        """Build a ``<var>`` element from a variable dict.

        Expected variable dict keys:

        * ``name``             — short variable code (e.g. ``"Q1"``, ``"SEX"``)
        * ``label``            — full variable label
        * ``question``         — verbatim question text
        * ``filter_condition`` — skip/filter instruction (e.g. "Q3で『はい』の方のみ")
        * ``categories``       — list of ``{"value": ..., "label": ...}`` dicts
        * ``format``           — ``"numeric"`` / ``"character"`` / ``"date"``
        """
        var_name = _str_or_none(var.get("name")) or f"V{index}"
        var_id = f"V{index}"

        # <var ID="V1" name="Q1">
        var_el = ET.SubElement(data_dscr, _q("var"), ID=var_id, name=var_name)

        # <labl> — variable label
        label = _str_or_none(var.get("label"))
        if label:
            _sub(var_el, "labl", text=label)

        # <qstn><qstnLit> and/or <filtrInstr> — question text + filter condition (C2)
        question = _str_or_none(var.get("question"))
        filter_cond = _str_or_none(var.get("filter_condition"))
        if question or filter_cond:
            qstn = _sub(var_el, "qstn")
            if question:
                _sub(qstn, "qstnLit", text=question)
            if filter_cond:
                _sub(qstn, "filtrInstr", text=filter_cond)

        # <catgry> — response categories
        categories = var.get("categories")
        if isinstance(categories, list):
            for cat in categories:
                if not isinstance(cat, dict):
                    continue
                cat_val = _str_or_none(cat.get("value"))
                cat_label = _str_or_none(cat.get("label"))
                if cat_val is None and cat_label is None:
                    continue
                catgry = _sub(var_el, "catgry")
                if cat_val is not None:
                    _sub(catgry, "catValu", text=cat_val)
                if cat_label:
                    _sub(catgry, "labl", text=cat_label)

        # <varFormat> — storage type
        fmt = _str_or_none(var.get("format"))
        if fmt:
            fmt_normalised = _normalise_format(fmt)
            ET.SubElement(var_el, _q("varFormat"), type=fmt_normalised)


# ---------------------------------------------------------------------------
# Private utilities
# ---------------------------------------------------------------------------

_FORMAT_MAP = {
    "numeric": "numeric",
    "number": "numeric",
    "int": "numeric",
    "integer": "numeric",
    "float": "numeric",
    "double": "numeric",
    "decimal": "numeric",
    "string": "character",
    "text": "character",
    "character": "character",
    "char": "character",
    "date": "date",
    "datetime": "date",
    "timestamp": "date",
}


def _normalise_format(fmt: str) -> str:
    """Map free-text format strings to DDI varFormat type values."""
    return _FORMAT_MAP.get(fmt.lower().strip(), fmt)


def _sanitise_id(raw_id: str) -> str:
    """Convert an arbitrary string to an XML-safe ID (no spaces, safe chars).

    DDI IDs are used as XML ``ID`` attributes and must be valid XML NCNames.
    This function replaces unsafe characters with hyphens and ensures the
    result starts with a letter or underscore.
    """
    import re

    sanitised = re.sub(r"[^A-Za-z0-9._\-]", "-", raw_id)
    # XML NCName must not start with a digit or hyphen
    if sanitised and (sanitised[0].isdigit() or sanitised[0] == "-"):
        sanitised = "id-" + sanitised
    return sanitised or "ddi-unknown"
