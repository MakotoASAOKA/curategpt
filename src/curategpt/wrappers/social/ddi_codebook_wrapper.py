"""Wrapper for DDI-Codebook metadata via OAI-PMH."""

import logging
import time
from dataclasses import dataclass, field
from typing import ClassVar, Dict, List, Optional, Union
from urllib.parse import urlencode

import requests
import xmltodict

from curategpt.wrappers.base_wrapper import BaseWrapper

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known OAI-PMH endpoints for major social science repositories
# ---------------------------------------------------------------------------
GESIS_OAI_URL = "https://api.gesis.org/DBK/OAI-PMH"
ICPSR_OAI_URL = "https://www.icpsr.umich.edu/icpsrweb/neutral/oai/studies"
UKDS_OAI_URL = "https://oai.ukdataservice.ac.uk/oai/provider"
SSJDA_OAI_URL = "https://ssjda.iss.u-tokyo.ac.jp/oai"


# ---------------------------------------------------------------------------
# XML / xmltodict helper utilities
# ---------------------------------------------------------------------------

def _ensure_list(value) -> List:
    """Ensure a value is always a list.

    xmltodict returns a dict for single child elements and a list for
    multiple child elements.  This helper normalises both cases.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _get_text(obj: Union[str, Dict, None]) -> Optional[str]:
    """Extract text content from an xmltodict element.

    Handles three cases:
    - plain string  -> returned as-is
    - {'#text': ...} -> returns the text value
    - None           -> returns None
    """
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj.strip() or None
    if isinstance(obj, dict):
        text = obj.get("#text") or obj.get("@label")
        return text.strip() if isinstance(text, str) else None
    return None


# ---------------------------------------------------------------------------
# Main wrapper class
# ---------------------------------------------------------------------------

@dataclass
class DDICodebookWrapper(BaseWrapper):
    """
    A wrapper to provide a search facade over DDI-Codebook metadata
    via OAI-PMH (Open Archives Initiative Protocol for Metadata Harvesting).

    DDI-Codebook (Data Documentation Initiative, version 2.x) is the
    international XML standard for documenting social science datasets.
    This wrapper harvests study-level metadata from OAI-PMH endpoints
    and converts it into flat Python dicts suitable for vector storage
    and retrieval-augmented generation (RAG) in CurateGPT.

    Supported repositories (any OAI-PMH endpoint serving DDI is accepted):

    - GESIS Leibniz Institute for the Social Sciences (default)
    - ICPSR (Inter-university Consortium for Political and Social Research)
    - UK Data Service
    - Social Science Japan Data Archive (SSJDA)

    Usage (CLI)::

        curategpt view index -c ddi_gesis --view ddi_codebook \\
            --init-with "{oai_base_url: 'https://api.gesis.org/DBK/OAI-PMH'}"

    Usage (Python)::

        from curategpt.wrappers.social.ddi_codebook_wrapper import DDICodebookWrapper
        wrapper = DDICodebookWrapper(oai_base_url=GESIS_OAI_URL)
        for obj, score, meta in wrapper.search("income inequality panel survey"):
            print(obj["title"])
    """

    name: ClassVar[str] = "ddi_codebook"

    default_object_type: str = "Study"
    default_embedding_model: str = "openai:"

    oai_base_url: str = field(default=GESIS_OAI_URL)
    """OAI-PMH base endpoint URL of the target repository."""

    metadata_prefix: str = field(default="oai_ddi")
    """OAI-PMH metadataPrefix for DDI.
    Common values: 'oai_ddi' (GESIS), 'ddi' (ICPSR), 'ddi25' (DDI 2.5)."""

    oai_set: Optional[str] = field(default=None)
    """Restrict harvesting to a specific OAI-PMH set (optional)."""

    max_records: int = field(default=100)
    """Maximum records returned per external_search() call."""

    request_delay: float = field(default=0.5)
    """Polite delay between HTTP requests (seconds)."""

    from_date: Optional[str] = field(default=None)
    """Harvest records modified on or after this date (ISO 8601: YYYY-MM-DD).
    Used for incremental updates.  None means harvest from the beginning."""

    until_date: Optional[str] = field(default=None)
    """Harvest records modified on or before this date (ISO 8601: YYYY-MM-DD).
    None means no upper bound."""

    session: requests.Session = field(default_factory=lambda: requests.Session())

    # ---------------------------------------------------------------------------
    # BaseWrapper interface
    # ---------------------------------------------------------------------------

    def objects(
        self,
        collection: str = None,
        object_ids=None,
        from_date: Optional[str] = None,
        until_date: Optional[str] = None,
        oai_set: Optional[str] = None,
        **kwargs,
    ):
        """
        Yield ALL DDI-Codebook records from the OAI-PMH endpoint.

        This method is used by the CLI ``curategpt view index`` command to
        pre-populate a ChromaDB collection with DDI study examples.  Unlike
        ``external_search()``, there is no ``max_records`` cap: every record
        returned by the server is yielded, enabling a complete snapshot of
        the repository.

        Incremental harvesting is supported via *from_date* / *until_date*,
        which map directly to the OAI-PMH ``from`` and ``until`` parameters.
        This allows you to refresh a collection by harvesting only records
        that have changed since the last run.

        Example — full harvest (CLI)::

            curategpt view index -c ddi_gesis -m openai: --view ddi_codebook \\
                --init-with "{oai_base_url: 'https://api.gesis.org/DBK/OAI-PMH'}"

        Example — incremental update (CLI)::

            curategpt view index -c ddi_gesis -m openai: --view ddi_codebook \\
                --init-with "{oai_base_url: 'https://api.gesis.org/DBK/OAI-PMH', \\
                              from_date: '2024-01-01'}"

        Example — specific IDs (Python)::

            wrapper = DDICodebookWrapper(oai_base_url=GESIS_OAI_URL)
            for obj in wrapper.objects(object_ids=["oai:gesis.org:ZA2800"]):
                print(obj["title"])

        :param collection: Ignored (present for API compatibility).
        :param object_ids: Optional iterable of OAI-PMH identifiers.
                           When provided, only those records are fetched.
        :param from_date: Override instance ``from_date`` for this call.
        :param until_date: Override instance ``until_date`` for this call.
        :param oai_set: Override instance ``oai_set`` for this call.
        :yield: Parsed DDI study dicts, one per OAI-PMH record.
        """
        if object_ids is not None:
            yield from self.objects_by_ids(list(object_ids))
            return

        target_set = oai_set if oai_set is not None else self.oai_set
        effective_from = from_date if from_date is not None else self.from_date
        effective_until = until_date if until_date is not None else self.until_date

        yield from self._harvest_generator(
            oai_set=target_set,
            from_date=effective_from,
            until_date=effective_until,
        )

    def external_search(
        self, text: str, expand: bool = True, limit: int = None, **kwargs
    ) -> List[Dict]:
        """
        Harvest a bounded batch of DDI-Codebook records from the OAI-PMH endpoint.

        Because OAI-PMH is a harvesting protocol (not a keyword search API),
        this method fetches a batch of records from the configured endpoint
        and returns them for vector-based re-ranking inside
        ``BaseWrapper.search()``.

        When *expand* is True and an LLM extractor is available, the query
        text is used to select the most relevant OAI-PMH set, narrowing the
        harvest to a topically relevant subset.

        :param text: Query text used for optional set selection.
        :param expand: If True, use LLM to map query to an OAI-PMH set.
        :param limit: Maximum records to return (overrides ``max_records``).
        :return: List of parsed DDI study dicts.
        """
        target_set = self.oai_set
        if expand and self.extractor is not None and target_set is None:
            target_set = self._suggest_set(text)

        max_records = limit if limit is not None else self.max_records
        return self._list_records(oai_set=target_set, max_records=max_records)

    def objects_by_ids(self, object_ids: List[str]) -> List[Dict]:
        """
        Fetch specific DDI records by OAI-PMH identifier.

        :param object_ids: OAI-PMH identifiers
                           (e.g. ``['oai:gesis.org:ZA2800']``).
        :return: List of parsed DDI study dicts.
        """
        results = []
        for oid in object_ids:
            record = self._get_record(oid)
            if record:
                results.append(record)
        return results

    # ---------------------------------------------------------------------------
    # OAI-PMH protocol helpers
    # ---------------------------------------------------------------------------

    def _oai_request(self, params: Dict) -> Dict:
        """Execute a single OAI-PMH HTTP request and return parsed XML as dict."""
        time.sleep(self.request_delay)
        response = self.session.get(self.oai_base_url, params=params)
        if not response.ok:
            raise ValueError(
                f"OAI-PMH request failed ({response.status_code}): "
                f"{self.oai_base_url}?{urlencode(params)}"
            )
        return xmltodict.parse(response.text)

    def _harvest_generator(
        self,
        oai_set: Optional[str] = None,
        from_date: Optional[str] = None,
        until_date: Optional[str] = None,
    ):
        """Generator that yields ALL matching DDI records via OAI-PMH ListRecords.

        Used by ``objects()`` for full / incremental harvests.  No upper
        bound on the number of records: pagination is followed automatically
        via resumption tokens until the server signals completion.

        :param oai_set: OAI-PMH set identifier to restrict harvesting.
        :param from_date: Harvest records modified on or after this date (YYYY-MM-DD).
        :param until_date: Harvest records modified on or before this date (YYYY-MM-DD).
        :yield: Parsed DDI study dicts.
        """
        params: Dict = {
            "verb": "ListRecords",
            "metadataPrefix": self.metadata_prefix,
        }
        if oai_set:
            params["set"] = oai_set
        if from_date:
            params["from"] = from_date
        if until_date:
            params["until"] = until_date

        total_yielded = 0
        resumption_token: Optional[str] = None

        while True:
            if resumption_token:
                # When resuming, only the token is needed
                params = {"verb": "ListRecords", "resumptionToken": resumption_token}

            try:
                data = self._oai_request(params)
            except Exception as exc:
                logger.error(f"OAI-PMH ListRecords failed: {exc}")
                return

            oai_root = data.get("OAI-PMH", {})
            error = oai_root.get("error")
            if error:
                error_code = error.get("@code", "") if isinstance(error, dict) else str(error)
                if error_code == "noRecordsMatch":
                    logger.info("OAI-PMH: no records matched the request parameters.")
                else:
                    logger.error(f"OAI-PMH error response: {error}")
                return

            list_records = oai_root.get("ListRecords", {})
            raw_records = _ensure_list(list_records.get("record", []))

            for raw in raw_records:
                parsed = self._parse_oai_record(raw)
                if parsed:
                    total_yielded += 1
                    if total_yielded % 100 == 0:
                        logger.info(
                            f"Harvested {total_yielded} DDI records so far "
                            f"from {self.oai_base_url} ..."
                        )
                    yield parsed

            # Follow resumption token (OAI-PMH pagination)
            rt = list_records.get("resumptionToken")
            if rt and isinstance(rt, dict):
                # The token element may carry a completeListSize attribute
                complete_size = rt.get("@completeListSize")
                if complete_size:
                    logger.info(
                        f"Repository reports {complete_size} total records. "
                        f"Harvested {total_yielded} so far."
                    )
                resumption_token = rt.get("#text")
            elif rt and isinstance(rt, str) and rt.strip():
                resumption_token = rt.strip()
            else:
                break  # server signals no more pages

        logger.info(
            f"Harvest complete: {total_yielded} DDI records from {self.oai_base_url}"
        )

    def _list_records(
        self,
        oai_set: Optional[str] = None,
        max_records: int = 100,
        from_date: Optional[str] = None,
        until_date: Optional[str] = None,
    ) -> List[Dict]:
        """Harvest a bounded batch of records via the OAI-PMH ``ListRecords`` verb.

        Collects up to *max_records* objects, following resumption tokens as
        needed, then returns the result as a list.  Used by
        ``external_search()`` for interactive queries.

        For an unbounded full harvest, use ``_harvest_generator()`` instead.

        :param oai_set: OAI-PMH set to restrict harvesting.
        :param max_records: Stop after collecting this many records.
        :param from_date: Harvest records modified on or after this date (YYYY-MM-DD).
        :param until_date: Harvest records modified on or before this date (YYYY-MM-DD).
        :return: List of parsed DDI study dicts.
        """
        records: List[Dict] = []
        for rec in self._harvest_generator(
            oai_set=oai_set,
            from_date=from_date,
            until_date=until_date,
        ):
            records.append(rec)
            if len(records) >= max_records:
                break

        logger.info(f"Collected {len(records)} DDI records from {self.oai_base_url}")
        return records

    def _get_record(self, identifier: str) -> Optional[Dict]:
        """Fetch a single DDI record via the OAI-PMH ``GetRecord`` verb."""
        params = {
            "verb": "GetRecord",
            "identifier": identifier,
            "metadataPrefix": self.metadata_prefix,
        }
        try:
            data = self._oai_request(params)
            raw = (
                data.get("OAI-PMH", {})
                .get("GetRecord", {})
                .get("record", {})
            )
            return self._parse_oai_record(raw)
        except Exception as exc:
            logger.error(f"Failed to fetch DDI record '{identifier}': {exc}")
            return None

    def _list_sets(self) -> List[Dict]:
        """List all OAI-PMH sets available at the endpoint."""
        try:
            data = self._oai_request({"verb": "ListSets"})
            raw_sets = _ensure_list(
                data.get("OAI-PMH", {}).get("ListSets", {}).get("set", [])
            )
            return [
                {
                    "setSpec": s.get("setSpec", ""),
                    "setName": _get_text(s.get("setName")),
                }
                for s in raw_sets
                if isinstance(s, dict)
            ]
        except Exception as exc:
            logger.warning(f"Could not list OAI-PMH sets: {exc}")
            return []

    # ---------------------------------------------------------------------------
    # OAI-PMH record → dict transformation
    # ---------------------------------------------------------------------------

    def _parse_oai_record(self, raw: Dict) -> Optional[Dict]:
        """Unwrap an OAI-PMH ``<record>`` element and delegate to
        ``objects_from_dict()``."""
        if not raw:
            return None

        header = raw.get("header", {})
        # Skip records that have been soft-deleted by the repository
        if header.get("@status") == "deleted":
            return None

        oai_id = header.get("identifier", "")
        metadata = raw.get("metadata", {})

        # The codeBook element may appear under several namespace-qualified
        # keys depending on how xmltodict has handled the namespace prefix.
        codebook = (
            metadata.get("codeBook")
            or metadata.get("codebook")
            or metadata.get("ddi:codeBook")
            or metadata.get("ns0:codeBook")
            or {}
        )
        if not codebook:
            logger.warning(f"No codeBook element found in OAI record: {oai_id}")
            return None

        return self.objects_from_dict(codebook, oai_id=oai_id)

    def objects_from_dict(self, codebook: Dict, oai_id: str = "") -> Dict:
        """
        Convert a DDI-Codebook dict (produced by ``xmltodict``) into a
        flat CurateGPT-compatible study record.

        The mapping follows the DDI-Codebook 2.5 schema structure:

        * ``docDscr`` — codebook-level documentation metadata
        * ``stdyDscr`` — study description (title, abstract, methodology …)
        * ``fileDscr`` — data file description
        * ``dataDscr`` — variable-level description

        :param codebook: Parsed ``<codeBook>`` element from xmltodict.
        :param oai_id: OAI-PMH record identifier (used as ``id``).
        :return: Flat dict ready for ChromaDB ingestion.
        """
        study: Dict = {}

        # ---- Top-level sections ----
        doc_dscr = codebook.get("docDscr") or {}
        std_dscr = codebook.get("stdyDscr") or {}
        file_dscr = codebook.get("fileDscr") or {}
        data_dscr = codebook.get("dataDscr") or {}

        # ================================================================
        # docDscr  —  codebook-level citation
        # ================================================================
        doc_citation = doc_dscr.get("citation") or {}
        doc_titl_stmt = doc_citation.get("titlStmt") or {}

        codebook_id = _get_text(doc_titl_stmt.get("IDNo"))
        study["id"] = oai_id or (f"ddi:{codebook_id}" if codebook_id else "")

        # ================================================================
        # stdyDscr/citation  —  study-level bibliographic info
        # ================================================================
        std_citation = std_dscr.get("citation") or {}
        std_titl_stmt = std_citation.get("titlStmt") or {}

        study["title"] = (
            _get_text(std_titl_stmt.get("titl"))
            or _get_text(doc_titl_stmt.get("titl"))
        )

        alt_titl = std_titl_stmt.get("altTitl")
        if alt_titl:
            study["alternative_title"] = _get_text(alt_titl)

        # Study accession numbers (may come from multiple agencies)
        id_no_list = _ensure_list(std_titl_stmt.get("IDNo"))
        study_ids: Dict[str, str] = {}
        for item in id_no_list:
            val = _get_text(item)
            if val:
                agency = item.get("@agency", "unknown") if isinstance(item, dict) else "unknown"
                study_ids[agency] = val
        if study_ids:
            study["study_ids"] = study_ids

        # Principal investigators
        rsp_stmt = std_citation.get("rspStmt") or {}
        authors = [_get_text(a) for a in _ensure_list(rsp_stmt.get("AuthEnty")) if a]
        if authors:
            study["principal_investigators"] = authors

        # Funding agencies
        prod_stmt = std_citation.get("prodStmt") or {}
        funders = [_get_text(f) for f in _ensure_list(prod_stmt.get("fundAg")) if f]
        if funders:
            study["funding_agencies"] = funders

        # Publisher / distributor
        distrib = _ensure_list((std_citation.get("distStmt") or {}).get("distrbtr"))
        if distrib:
            study["distributors"] = [_get_text(d) for d in distrib if d]

        # ================================================================
        # stdyDscr/stdyInfo  —  study content description
        # ================================================================
        std_info = std_dscr.get("stdyInfo") or {}

        abstract = std_info.get("abstract")
        if abstract:
            study["abstract"] = _get_text(abstract)

        subject = std_info.get("subject") or {}

        keywords = [_get_text(k) for k in _ensure_list(subject.get("keyword")) if k]
        if keywords:
            study["keywords"] = keywords

        topic_classes = [
            _get_text(t) for t in _ensure_list(subject.get("topcClas")) if t
        ]
        if topic_classes:
            study["topic_classification"] = topic_classes

        # Summary description (geographic, temporal, methodological)
        sum_dscr = std_info.get("sumDscr") or {}

        time_periods = self._parse_date_elements(_ensure_list(sum_dscr.get("timePrd")))
        if time_periods:
            study["time_periods"] = time_periods

        coll_dates = self._parse_date_elements(_ensure_list(sum_dscr.get("collDate")))
        if coll_dates:
            study["collection_dates"] = coll_dates

        nations = [_get_text(n) for n in _ensure_list(sum_dscr.get("nation")) if n]
        if nations:
            study["nations"] = nations

        geo_cover = [
            _get_text(g) for g in _ensure_list(sum_dscr.get("geogCover")) if g
        ]
        if geo_cover:
            study["geographic_coverage"] = geo_cover

        universe = _get_text(sum_dscr.get("universe"))
        if universe:
            study["universe"] = universe

        anly_units = [
            _get_text(a) for a in _ensure_list(sum_dscr.get("anlyUnit")) if a
        ]
        if anly_units:
            study["analysis_unit"] = anly_units

        samp_proc = _get_text(sum_dscr.get("sampProc"))
        if samp_proc:
            study["sampling_procedure"] = samp_proc

        # ================================================================
        # stdyDscr/method  —  data collection methodology
        # ================================================================
        method = std_dscr.get("method") or {}
        data_coll = method.get("dataColl") or {}

        time_meth = _get_text(data_coll.get("timeMeth"))
        if time_meth:
            study["time_method"] = time_meth

        coll_modes = [
            _get_text(c) for c in _ensure_list(data_coll.get("collMode")) if c
        ]
        if coll_modes:
            study["collection_mode"] = coll_modes

        coll_situ = _get_text(data_coll.get("collSitu"))
        if coll_situ:
            study["collection_situation"] = coll_situ

        instrument = _get_text(data_coll.get("instrumentDevelopment"))
        if instrument:
            study["instrument_development"] = instrument

        weight = _get_text(data_coll.get("weight"))
        if weight:
            study["weighting"] = weight

        clean_ops = _get_text(data_coll.get("cleanOps"))
        if clean_ops:
            study["cleaning_operations"] = clean_ops

        # ================================================================
        # stdyDscr/dataAccs  —  access conditions
        # ================================================================
        data_accs = std_dscr.get("dataAccs") or {}
        set_avail = data_accs.get("setAvail") or {}
        accs_plac = set_avail.get("accsPlac")
        if accs_plac:
            study["access_place"] = (
                accs_plac.get("@URI") or _get_text(accs_plac)
                if isinstance(accs_plac, dict)
                else _get_text(accs_plac)
            )

        use_stmt = data_accs.get("useStmt") or {}
        restrctn = _get_text(use_stmt.get("restrctn"))
        if restrctn:
            study["access_conditions"] = restrctn

        spec_perm = _get_text(use_stmt.get("specPerm"))
        if spec_perm:
            study["special_permissions"] = spec_perm

        # ================================================================
        # fileDscr  —  data file description
        # ================================================================
        file_txt = file_dscr.get("fileTxt") or {}
        file_name = _get_text(file_txt.get("fileName"))
        if file_name:
            study["file_name"] = file_name

        file_type = _get_text(file_txt.get("fileType"))
        if file_type:
            study["file_type"] = file_type

        file_cont = _get_text(file_txt.get("fileCont"))
        if file_cont:
            study["file_content_description"] = file_cont

        # ================================================================
        # dataDscr  —  variable descriptions
        # ================================================================
        variables = _ensure_list(data_dscr.get("var"))
        if variables:
            study["variable_count"] = len(variables)
            study["variables"] = self._parse_variables(variables)

        # Remove keys with empty / None values before returning
        return {k: v for k, v in study.items() if v is not None and v != [] and v != {}}

    # ---------------------------------------------------------------------------
    # DDI element parsers
    # ---------------------------------------------------------------------------

    def _parse_date_elements(self, elements: List) -> List[Dict]:
        """Parse DDI ``<timePrd>`` / ``<collDate>`` elements into dicts."""
        result = []
        for elem in elements:
            if isinstance(elem, dict):
                entry = {
                    "event": elem.get("@event"),
                    "date": elem.get("@date"),
                    "label": _get_text(elem),
                }
            else:
                entry = {"label": _get_text(elem)}
            cleaned = {k: v for k, v in entry.items() if v}
            if cleaned:
                result.append(cleaned)
        return result

    def _parse_variables(self, variables: List[Dict], cap: int = 200) -> List[Dict]:
        """
        Parse DDI ``<var>`` elements into minimal dicts for RAG context.

        Only the first *cap* variables are included to avoid overloading
        the embedding context window.

        :param variables: List of raw ``<var>`` dicts from xmltodict.
        :param cap: Maximum number of variables to include.
        :return: List of variable summary dicts.
        """
        parsed = []
        for var in variables[:cap]:
            if not isinstance(var, dict):
                continue

            v: Dict = {}

            name = var.get("@name")
            if name:
                v["name"] = name

            label = _get_text(var.get("labl"))
            if label:
                v["label"] = label

            # Question text (from questionnaire)
            qstn = var.get("qstn") or {}
            if isinstance(qstn, dict):
                q_text = _get_text(qstn.get("qstnLit"))
                if q_text:
                    v["question"] = q_text

            # Universe for this variable
            var_universe = _get_text(var.get("universe"))
            if var_universe:
                v["universe"] = var_universe

            # Category codes and labels (for categorical / nominal variables)
            categories = _ensure_list(var.get("catgry"))
            if categories:
                cats = []
                for cat in categories:
                    if not isinstance(cat, dict):
                        continue
                    cat_entry = {
                        "value": _get_text(cat.get("catValu")),
                        "label": _get_text(cat.get("labl")),
                    }
                    cleaned_cat = {k: val for k, val in cat_entry.items() if val}
                    if cleaned_cat:
                        cats.append(cleaned_cat)
                if cats:
                    v["categories"] = cats

            # Variable format type (numeric / character / date)
            var_format = var.get("varFormat") or {}
            if isinstance(var_format, dict):
                fmt = var_format.get("@type")
                if fmt:
                    v["format"] = fmt

            if v:
                parsed.append(v)

        return parsed

    # ---------------------------------------------------------------------------
    # LLM-assisted set suggestion
    # ---------------------------------------------------------------------------

    def _suggest_set(self, text: str) -> Optional[str]:
        """Use the LLM extractor to choose the best OAI-PMH set for a query.

        :param text: User query / research topic.
        :return: ``setSpec`` string or ``None`` if unavailable.
        """
        sets = self._list_sets()
        if not sets:
            return None

        sets_str = "\n".join(
            f"- {s['setSpec']}: {s['setName']}" for s in sets[:50]
        )
        system = (
            "You are a social science data archivist. "
            "Given a research topic, select the single most relevant OAI-PMH set."
        )
        prompt = (
            f"Available sets:\n{sets_str}\n\n"
            f"Research topic: {text}\n\n"
            "Return only the setSpec value, nothing else."
        )
        try:
            response = self.extractor.model.prompt(prompt, system=system)
            suggested = response.text().strip().splitlines()[0].strip()
            if suggested:
                logger.info(f"LLM suggested OAI-PMH set '{suggested}' for query: {text}")
                return suggested
        except Exception as exc:
            logger.warning(f"LLM set suggestion failed: {exc}")
        return None
