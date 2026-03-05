"""DDI Controlled Vocabulary (CV) automatic mapper.

Maps free-text field values returned by the LLM to standardised
DDI Alliance CV codes.  Regex patterns are matched case-insensitively
against the normalised field string; the first match wins.

Supported fields and CV versions
---------------------------------
field                cv_name                  version
-----------          --------------------     -------
collection_mode   →  ModeOfCollection         3.0
time_method       →  TimeMethod               1.2
analysis_unit     →  AnalysisUnit             1.1
sampling_procedure→  SamplingProcedure        1.0  (top-level categories only)
topic_classification→ TopicClassification     4.2.2 (CESSDA)

Usage
-----
::

    from curategpt.wrappers.social.ddi_cv_mapper import apply_cv_mappings

    ddi = agent.extract_ddi_from_file("report.pdf")
    ddi = apply_cv_mappings(ddi)

    print(ddi["collection_mode"])        # 郵送調査  (original, preserved)
    print(ddi["collection_mode_cv"])     # SelfAdministeredQuestionnaire.Paper
    print(ddi["collection_mode_cv_list_id"])   # ModeOfCollection
    print(ddi["collection_mode_cv_list_ver"])  # 3.0

    # List field: topic_classification_cv is a list of codes
    print(ddi["topic_classification_cv"])      # ["Health.PublicHealth", "Politics.Elections"]

After ``apply_cv_mappings()`` the dict contains three parallel keys for each
mapped field:

* ``{field}_cv``            – code string (or *None*) for scalar fields;
                              list of codes (element may be *None*) for list fields
* ``{field}_cv_list_id``    – codeListID attribute value
* ``{field}_cv_list_ver``   – codeListVersionID attribute value
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pattern tables
# Each entry: (regex_pattern, cv_code)
# Patterns are applied in ORDER; more specific patterns must come FIRST.
# All patterns are matched with re.IGNORECASE.
# ---------------------------------------------------------------------------

# ModeOfCollection CV 3.0
# https://ddialliance.org/Specification/DDI-CV/ModeOfCollection_3.0.html
_MODE_PATTERNS: List[Tuple[str, str]] = [
    # --- Computer-assisted subtypes (specific → generic) ---
    (r"capi|computer.?assisted\s+personal", "Interview.FaceToFace.CAPI"),
    (r"cati|computer.?assisted\s+telephone", "Interview.Telephone.CATI"),
    (r"cawi|computer.?assisted\s+web", "SelfAdministeredQuestionnaire.CAWI"),
    (r"casi|computer.?assisted\s+self", "SelfAdministeredQuestionnaire.CASI"),
    # --- Face-to-face ---
    (r"face.?to.?face|対面|訪問面接|個人面接|interviewer.?administered", "Interview.FaceToFace"),
    # --- Telephone ---
    (r"telephone|電話|電話調査|電話面接|phone\s+interview", "Interview.Telephone"),
    # --- Web / online ---
    (r"online|web\s+survey|internet\s+survey|オンライン|ウェブ調査|インターネット調査",
     "SelfAdministeredQuestionnaire.CAWI"),
    # --- Paper self-administered (郵送・留置は日本語調査で最多) ---
    (r"郵送|郵便.*調査|mail.*questionnaire|postal.*survey|留置|配票|留め置き",
     "SelfAdministeredQuestionnaire.Paper"),
    (r"self.?admin.*paper|paper.*self.?admin|紙.*自記|自記.*紙",
     "SelfAdministeredQuestionnaire.Paper"),
    # --- General self-administered ---
    (r"self.?administered|自記式|自計式", "Interview.SelfAdministered"),
    # --- Email ---
    (r"e.?mail\s*(survey|questionnaire)?|メール調査|電子メール", "Interview.Email"),
    # --- Focus group ---
    (r"focus.?group|グループインタビュー|フォーカスグループ", "FocusGroup"),
    # --- Observation ---
    (r"observation|観察調査|観察", "Observation"),
    # --- Records abstraction ---
    (r"record.*abstraction|administrative.*record|行政記録|記録抽出", "RecordsAbstraction"),
    # --- Mixed / Other ---
    (r"mixed.?mode|複合モード|混合モード", "Other"),
]

# TimeMethod CV 1.2
# https://ddialliance.org/Specification/DDI-CV/TimeMethod_1.2.html
_TIME_PATTERNS: List[Tuple[str, str]] = [
    # Panel (specific longitudinal subtype)
    (r"panel\s*(study|survey)?|パネル調査|パネル", "Longitudinal.Panel"),
    # Cohort
    (r"cohort|コホート研究|コホート", "Longitudinal.Cohort"),
    # Trend / repeated cross-section
    (r"trend\s*(study)?|繰り返し横断|反復横断|トレンド調査|トレンド", "Longitudinal.Trend"),
    # Other longitudinal
    (r"longitudinal|縦断調査|縦断", "Longitudinal"),
    # Time series
    (r"time.?series|時系列データ|時系列", "TimeSeries"),
    # Cross-section (most common; keep last as catch-all for "one-time")
    (r"cross.?section|横断調査|横断|一時点|一回限り|単発|one.?time", "CrossSection"),
]

# AnalysisUnit CV 1.1
# https://ddialliance.org/Specification/DDI-CV/AnalysisUnit_1.1.html
_UNIT_PATTERNS: List[Tuple[str, str]] = [
    (r"household|世帯|家計|hhld", "HouseholdUnit"),
    (r"family\s*group|家族グループ|家族世帯", "Family.FamilyGroup"),
    (r"\bfamily\b|家族|家庭", "Family"),
    (r"individual|個人|個人単位", "Individual"),
    (r"organization|enterprise|company|企業|組織|事業所|機関|法人", "Organization"),
    (r"geographic|地域|地区|市区町村|都道府県|prefecture|municipality", "GeographicUnit"),
    (r"\bgroup\b|集団|グループ", "Group"),
    (r"text\s*unit|テキスト単位|文書単位", "TextUnit"),
    (r"event|イベント|出来事|事象", "Event"),
    (r"\bobject\b|物品|モノ|製品", "Object"),
    (r"time\s*unit|時間単位|期間", "TimeUnit"),
]

# SamplingProcedure CV 1.0
# https://ddialliance.org/Specification/DDI-CV/SamplingProcedure_1.0.html
# Note: in DDI-Codebook 2.5 <sampProc> is free text, but CV codes are used as
# content for interoperability.
_SAMPLING_PATTERNS: List[Tuple[str, str]] = [
    # Full enumeration / census
    (r"census|悉皆|全数調査|全件調査|complete\s*enumeration", "TotalUniverse"),
    # Probability subtypes (specific → generic)
    (r"simple\s*random|単純無作為抽出|単純ランダム", "Probability.SimpleRandom"),
    (r"stratif|層化抽出|層別抽出|層化", "Probability.Stratified"),
    (r"cluster|クラスター抽出|多段抽出|multi.?stage", "Probability.Cluster"),
    (r"systematic|系統抽出|等間隔抽出", "Probability.Systematic"),
    (r"probability\s*(sampling)?|確率抽出|確率的標本|確率比例", "Probability"),
    # Non-probability subtypes
    (r"quota|割当抽出|クォータ", "NonProbability.Quota"),
    (r"purposive|有意抽出|目的的", "NonProbability.Purposive"),
    (r"snowball|スノーボール", "NonProbability.Snowball"),
    (r"convenience|便宜的抽出|便宜抽出", "NonProbability.Convenience"),
    (r"voluntary|自発的|自由参加", "NonProbability.VolunteerSample"),
    (r"non.?probab|非確率抽出|非確率", "NonProbability"),
    # Mixed
    (r"mixed\s*(method)?|混合抽出|組み合わせ", "Mixed"),
]

# TopicClassification CV 4.2.2  (CESSDA)
# https://vocabularies.cessda.eu/vocabulary/TopicClassification
# Sub-codes appear BEFORE their parent top-level code (specificity first).
_TOPIC_PATTERNS: List[Tuple[str, str]] = [
    # ── Demography sub-codes ──
    (r"census|センサス|国勢調査|人口調査", "Demography.Censuses"),
    (r"migration|immigration|emigration|移住|移民|人口移動|外国人", "Demography.Migration"),
    (r"morbidity|mortality|死亡率|罹患率|疾病率|生命表", "Demography.MorbidityAndMortality"),
    # Demography top-level (catch-all)
    (r"demography|demographic|vital statistics|population\s*(statistics|data|survey)?|人口統計|人口",
     "Demography"),

    # ── Economics sub-codes ──
    (r"consumption|consumer\s*behav|消費行動|消費者行動|消費", "Economics.ConsumptionAndConsumerBehaviour"),
    (r"economic\s*(condition|indicator)|景気|経済指標|経済状況|GD[PN]", "Economics.EconomicConditionsAndIndicators"),
    (r"economic\s*policy|public\s*expenditure|public\s*revenue|財政|経済政策|歳出|歳入",
     "Economics.EconomicPolicyPublicExpenditureAndRevenue"),
    (r"economic\s*(system|development)|経済開発|経済制度|開発経済", "Economics.EconomicSystemsAndDevelopment"),
    (r"income|property|investment|saving|所得|資産|貯蓄|投資|資本", "Economics.IncomePropertyAndInvestmentSaving"),
    # Economics top-level
    (r"economics?|economy|経済学|経済", "Economics"),

    # ── Education sub-codes ──
    (r"compulsory\s*education|pre.?school|preschool|義務教育|就学前|幼稚園|保育",
     "Education.CompulsoryAndPreschoolEducation"),
    (r"educational\s*policy|教育政策|教育行政", "Education.EducationalPolicy"),
    (r"higher\s*education|further\s*education|university|高等教育|大学|短大|専門学校",
     "Education.HigherAndFurtherEducation"),
    (r"lifelong|continuing\s*education|生涯学習|継続教育|社会教育", "Education.LifelongContinuingEducation"),
    (r"vocational|職業訓練|職業教育|専門訓練", "Education.VocationalEducationAndTraining"),
    # Education top-level
    (r"education|学校|教育|学習|就学", "Education"),

    # ── Health sub-codes ──
    (r"diet|nutrition|食事|栄養|食生活|食品", "Health.DietAndNutrition"),
    (r"drug\s*abuse|alcohol|smoking|薬物乱用|飲酒|喫煙|アルコール|たばこ",
     "Health.DrugAbuseAlcoholAndSmoking"),
    (r"general\s*health|well.?being|health\s*(status|condition)|主観的健康|健康状態|健康度|QOL",
     "Health.GeneralHealthAndWellbeing"),
    (r"health\s*care\s*service|health\s*polic|medical\s*service|医療サービス|保健サービス|医療政策|保健政策",
     "Health.HealthCareServicesAndPolicies"),
    (r"medication|treatment|drug\s*therapy|薬物療法|治療|投薬|医薬", "Health.MedicationAndTreatment"),
    (r"occupational\s*health|産業保健|労働衛生|職業病", "Health.OccupationalHealth"),
    (r"physical\s*fitness|exercise|体力|運動|スポーツ医学|フィットネス", "Health.PhysicalFitnessAndExercise"),
    (r"public\s*health|公衆衛生|地域保健", "Health.PublicHealth"),
    (r"reproductive\s*health|sexuality|生殖保健|性と生殖|リプロダクティブ", "Health.ReproductiveHealth"),
    (r"symptoms?|pathological|symptom|signs?|症状|病態|徴候", "Health.SignsAndSymptomsPathologicalConditions"),
    (r"disease|disorder|medical\s*condition|疾患|疾病|病気|障害", "Health.SpecificDiseasesDisordersAndMedicalConditions"),
    (r"wounds?|injur|外傷|負傷|事故", "Health.WoundsAndInjuries"),
    # Health top-level
    (r"health|保健|医療|健康|衛生", "Health"),

    # ── History ──
    (r"history|historical|歴史|史学|近代史|戦後", "History"),

    # ── HousingAndLandUse sub-codes ──
    (r"land\s*use|urban\s*planning|land\s*planning|土地利用|都市計画|地域計画",
     "HousingAndLandUse.LandUseAndPlanning"),
    (r"housing|居住|住宅|住まい|住環境", "HousingAndLandUse.Housing"),
    # HousingAndLandUse top-level
    (r"housing|land\s*use|住宅|土地", "HousingAndLandUse"),

    # ── LabourAndEmployment sub-codes ──
    (r"employee\s*training|職員研修|社員研修|人材育成|OJT", "LabourAndEmployment.EmployeeTraining"),
    (r"labour\s*(and\s*)?employment\s*polic|雇用政策|労働政策", "LabourAndEmployment.LabourAndEmploymentPolicy"),
    (r"labour\s*relation|industrial\s*relation|労使関係|労働争議|組合",
     "LabourAndEmployment.LabourRelationsConflict"),
    (r"retirement|定年|退職|老後|年金", "LabourAndEmployment.Retirement"),
    (r"unemployment|失業|求職|ハローワーク", "LabourAndEmployment.Unemployment"),
    (r"working\s*condition|労働条件|労働環境|労働時間|賃金", "LabourAndEmployment.WorkingConditions"),
    (r"\bemployment\b|雇用|就業|就職", "LabourAndEmployment.Employment"),
    # LabourAndEmployment top-level
    (r"labour|labor|employment|work(place|force)?|労働|雇用|就業", "LabourAndEmployment"),

    # ── LawCrimeAndLegalSystems sub-codes ──
    (r"crime|law\s*enforcement|criminal|犯罪|治安|警察|刑事", "LawCrimeAndLegalSystems.CrimeAndLawEnforcement"),
    (r"legislation|legal\s*system|law\s*system|司法|法制度|立法|裁判",
     "LawCrimeAndLegalSystems.LegislationAndLegalSystems"),
    # LawCrimeAndLegalSystems top-level
    (r"law|crime|legal|司法|法律|犯罪|法", "LawCrimeAndLegalSystems"),

    # ── MediaCommunicationAndLanguage sub-codes ──
    (r"information\s*society|デジタル社会|情報化社会|情報社会", "MediaCommunicationAndLanguage.InformationSociety"),
    (r"language|linguistics|言語|言語学|語学", "MediaCommunicationAndLanguage.LanguageAndLinguistics"),
    (r"\bmedia\b|マスメディア|マスコミ|新聞|テレビ|SNS", "MediaCommunicationAndLanguage.Media"),
    (r"public\s*relations?|広報|PR|パブリシティ", "MediaCommunicationAndLanguage.PublicRelations"),
    # MediaCommunicationAndLanguage top-level
    (r"media|communication|言語|情報|通信|コミュニケーション", "MediaCommunicationAndLanguage"),

    # ── NaturalEnvironment sub-codes ──
    (r"energy|natural\s*resources?|エネルギー|天然資源|資源", "NaturalEnvironment.EnergyAndNaturalResources"),
    (r"environment.*conservation|conservation.*environment|環境保全|自然保護|生態系保全",
     "NaturalEnvironment.EnvironmentAndConservation"),
    (r"natural\s*landscape|自然景観|国立公園|自然公園", "NaturalEnvironment.NaturalLandscapes"),
    (r"plants?\s*and\s*animals?|flora|fauna|動植物|生物|野生動物", "NaturalEnvironment.PlantsAndAnimals"),
    # NaturalEnvironment top-level
    (r"natural\s*environment|environment|ecology|自然環境|環境|生態", "NaturalEnvironment"),

    # ── Politics sub-codes ──
    (r"conflict|security|peace|安全保障|国防|紛争|平和|軍事", "Politics.ConflictSecurityAndPeace"),
    (r"election|voting|ballot|選挙|投票|候補者|議員選挙", "Politics.Elections"),
    (r"government|political\s*system|政府|行政|政治システム|統治", "Politics.GovernmentPoliticalSystemsAndOrganisations"),
    (r"international\s*politics|international\s*organisation|国際政治|国際関係|外交",
     "Politics.InternationalPoliticsAndOrganisations"),
    (r"political\s*behav|political\s*attitude|political\s*particip|政治行動|政治意識|政治参加",
     "Politics.PoliticalBehaviourAndAttitudes"),
    (r"political\s*ideology|政治イデオロギー|政治思想|保守|革新|左派|右派",
     "Politics.PoliticalIdeology"),
    # Politics top-level
    (r"politics|political|政治|選挙|政党", "Politics"),

    # ── Psychology ──
    (r"psychology|psychological|mental\s*health|心理学|心理|精神|メンタルヘルス", "Psychology"),

    # ── ScienceAndTechnology sub-codes ──
    (r"biotechnology|バイオテクノロジー|生命工学|遺伝子", "ScienceAndTechnology.Biotechnology"),
    (r"information\s*technology|IT\b|ICT|digital|情報技術|デジタル|DX", "ScienceAndTechnology.InformationTechnology"),
    # ScienceAndTechnology top-level
    (r"science|technology|innovation|科学技術|科学|技術|イノベーション", "ScienceAndTechnology"),

    # ── SocialStratificationAndGroupings sub-codes ──
    (r"children|child\b|kids|子ども|子供|児童|小児", "SocialStratificationAndGroupings.Children"),
    (r"elderly|aged|older\s*people|高齢者|老人|シニア|高齢化", "SocialStratificationAndGroupings.Elderly"),
    (r"elites?|leadership|指導者|エリート|リーダーシップ", "SocialStratificationAndGroupings.ElitesAndLeadership"),
    (r"equality|inequality|social\s*exclusion|平等|不平等|格差|社会的排除",
     "SocialStratificationAndGroupings.EqualityInequalityAndSocialExclusion"),
    (r"family\s*life|marriage|家族生活|結婚|婚姻|家庭生活", "SocialStratificationAndGroupings.FamilyLifeAndMarriage"),
    (r"gender|gender\s*roles?|sex\s*difference|ジェンダー|性別|男女|女性|男性",
     "SocialStratificationAndGroupings.GenderAndGenderRoles"),
    (r"minorit|ethnic|マイノリティ|少数者|少数派|民族|エスニック",
     "SocialStratificationAndGroupings.Minorities"),
    (r"social\s*mobility|occupational\s*mobility|社会移動|職業移動|階層移動",
     "SocialStratificationAndGroupings.SocialAndOccupationalMobility"),
    (r"youth|young\s*people|adolescent|青少年|若者|青年|10代|20代",
     "SocialStratificationAndGroupings.Youth"),
    # SocialStratificationAndGroupings top-level
    (r"social\s*stratification|social\s*class|groupings?|社会階層|階層|集団|階級",
     "SocialStratificationAndGroupings"),

    # ── SocialWelfarePolicyAndSystems sub-codes ──
    (r"social\s*welfare\s*polic|社会福祉政策|社会保障政策", "SocialWelfarePolicyAndSystems.SocialWelfarePolicy"),
    (r"welfare\s*system|social\s*security\s*system|社会保障制度|社会福祉制度|年金制度",
     "SocialWelfarePolicyAndSystems.SocialWelfareSystemsStructures"),
    (r"social\s*service|福祉サービス|介護サービス|支援サービス",
     "SocialWelfarePolicyAndSystems.SpecificSocialServicesUseAndAvailability"),
    # SocialWelfarePolicyAndSystems top-level
    (r"social\s*welfare|welfare|social\s*security|社会福祉|福祉|社会保障|介護",
     "SocialWelfarePolicyAndSystems"),

    # ── SocietyAndCulture sub-codes ──
    (r"community|urban\s*life|rural\s*life|地域|都市|農村|まちづくり",
     "SocietyAndCulture.CommunityUrbanAndRuralLife"),
    (r"cultural\s*activit|cultural\s*particip|文化活動|文化参加|芸術活動",
     "SocietyAndCulture.CulturalActivitiesAndParticipation"),
    (r"cultural\s*identity|national\s*identity|文化的アイデンティティ|国民意識|ナショナル",
     "SocietyAndCulture.CulturalAndNationalIdentity"),
    (r"leisure|tourism|sport\b|余暇|観光|スポーツ|レジャー|旅行",
     "SocietyAndCulture.LeisureTourismAndSport"),
    (r"religion|values|宗教|価値観|信仰|道徳", "SocietyAndCulture.ReligionAndValues"),
    (r"social\s*behav|social\s*attitude|social\s*norm|社会的行動|社会意識|社会規範",
     "SocietyAndCulture.SocialBehaviourAndAttitudes"),
    (r"social\s*change|社会変動|社会変化", "SocietyAndCulture.SocialChange"),
    (r"social\s*condition|social\s*indicator|社会状況|社会指標|生活水準",
     "SocietyAndCulture.SocialConditionsAndIndicators"),
    (r"time\s*use|生活時間|時間利用|時間配分", "SocietyAndCulture.TimeUse"),
    # SocietyAndCulture top-level
    (r"society|culture|社会|文化|生活|習慣", "SocietyAndCulture"),

    # ── TradeIndustryAndMarkets sub-codes ──
    (r"agriculture|rural\s*industry|farming|農業|農村産業|農林水産",
     "TradeIndustryAndMarkets.AgricultureAndRuralIndustry"),
    (r"business|industrial\s*management|企業|産業管理|経営|事業所",
     "TradeIndustryAndMarkets.BusinessIndustrialManagementAndOrganisation"),
    (r"foreign\s*trade|international\s*trade|貿易|外国貿易|輸出入",
     "TradeIndustryAndMarkets.ForeignTrade"),
    # TradeIndustryAndMarkets top-level
    (r"trade|industry|market|産業|市場|流通|商業", "TradeIndustryAndMarkets"),

    # ── TransportAndTravel ──
    (r"transport|travel|交通|旅行|移動手段|道路|鉄道|航空", "TransportAndTravel"),

    # ── Other (catch-all) ──
    (r"other|その他|不明|未分類", "Other"),
]

# ---------------------------------------------------------------------------
# Field registry: maps dict key → (patterns, codeListID, codeListVersionID,
#                                   is_list)
# ---------------------------------------------------------------------------

_FIELD_REGISTRY: Dict[str, Dict[str, Any]] = {
    "collection_mode": {
        "patterns": _MODE_PATTERNS,
        "code_list_id": "ModeOfCollection",
        "code_list_ver": "3.0",
        "is_list": False,
    },
    "time_method": {
        "patterns": _TIME_PATTERNS,
        "code_list_id": "TimeMethod",
        "code_list_ver": "1.2",
        "is_list": False,
    },
    "analysis_unit": {
        "patterns": _UNIT_PATTERNS,
        "code_list_id": "AnalysisUnit",
        "code_list_ver": "1.1",
        "is_list": False,
    },
    "sampling_procedure": {
        "patterns": _SAMPLING_PATTERNS,
        # sampProc is free-text in DDI-Codebook; no formal codeListID attribute
        "code_list_id": None,
        "code_list_ver": None,
        "is_list": False,
    },
    "topic_classification": {
        "patterns": _TOPIC_PATTERNS,
        "code_list_id": "TopicClassification",
        "code_list_ver": "4.2.2",
        "is_list": True,
    },
}

# Public: exposes CV metadata for the XML serializer (avoids re-import of
# the full registry in every serializer call).
CV_FIELD_META: Dict[str, Tuple[Optional[str], Optional[str]]] = {
    field: (meta["code_list_id"], meta["code_list_ver"])
    for field, meta in _FIELD_REGISTRY.items()
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def map_to_cv(field: str, value: str) -> Optional[str]:
    """Map a free-text *value* for *field* to the DDI Alliance CV code.

    Parameters
    ----------
    field:
        One of the supported dict keys: ``"collection_mode"``,
        ``"time_method"``, ``"analysis_unit"``, ``"sampling_procedure"``,
        ``"topic_classification"``.
    value:
        Free-text string returned by the LLM.

    Returns
    -------
    str or None
        The CV code (e.g. ``"SelfAdministeredQuestionnaire.Paper"``), or
        ``None`` if no pattern matches.
    """
    meta = _FIELD_REGISTRY.get(field)
    if not meta or not isinstance(value, str):
        return None

    # Normalise: collapse whitespace, strip outer spaces
    normalized = re.sub(r"\s+", " ", value.strip())

    for pattern, code in meta["patterns"]:
        if re.search(pattern, normalized, re.IGNORECASE):
            return code

    return None


def apply_cv_mappings(ddi_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Enrich *ddi_dict* with DDI Alliance CV codes for vocabulary fields.

    For scalar fields the function stores three parallel keys:

    * ``{field}_cv``            – code string or *None*
    * ``{field}_cv_list_id``    – codeListID  (e.g. ``"ModeOfCollection"``)
    * ``{field}_cv_list_ver``   – codeListVersionID (e.g. ``"3.0"``)

    For list fields (e.g. ``topic_classification``) each item is mapped
    individually and ``{field}_cv`` holds a list of codes (elements may be
    *None* when no match is found).

    The original field values are **not** modified.

    Parameters
    ----------
    ddi_dict:
        DDI metadata dict (mutated in-place).

    Returns
    -------
    dict
        The same dict with ``_cv*`` keys added.
    """
    for field, meta in _FIELD_REGISTRY.items():
        val = ddi_dict.get(field)
        if not val:
            continue

        is_list = meta.get("is_list", False)

        if is_list:
            # val may be a list or a single string
            items: List[str] = val if isinstance(val, list) else [val]
            codes: List[Optional[str]] = []
            for item in items:
                if not isinstance(item, str) or not item.strip():
                    codes.append(None)
                    continue
                code = map_to_cv(field, item)
                codes.append(code)
                if code:
                    logger.info("CV mapping [%s]: %r → %r", field, item[:80], code)
                else:
                    logger.debug(
                        "CV mapping [%s]: no match for %r — original value kept",
                        field, item[:80],
                    )
            ddi_dict[f"{field}_cv"] = codes
            ddi_dict[f"{field}_cv_list_id"] = meta["code_list_id"]
            ddi_dict[f"{field}_cv_list_ver"] = meta["code_list_ver"]

        else:
            code = map_to_cv(field, str(val))
            ddi_dict[f"{field}_cv"] = code
            ddi_dict[f"{field}_cv_list_id"] = meta["code_list_id"]
            ddi_dict[f"{field}_cv_list_ver"] = meta["code_list_ver"]

            if code:
                logger.info(
                    "CV mapping [%s]: %r → %r",
                    field, str(val)[:80], code,
                )
            else:
                logger.debug(
                    "CV mapping [%s]: no match for %r — original value kept",
                    field, str(val)[:80],
                )

    return ddi_dict
