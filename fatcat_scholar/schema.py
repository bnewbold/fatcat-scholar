"""
Originally wrote these as dataclasses using pydantic.dataclasses, but we don't
get serialization for free with those. This is useful for things like
auto-conversion of datetime objects.
"""

import re
import datetime
from enum import Enum
from typing import Optional, List, Any, Dict

import ftfy
from bs4 import BeautifulSoup

# pytype: disable=import-error
from pydantic import BaseModel

# pytype: enable=import-error

from fatcat_openapi_client import ReleaseEntity, ReleaseContrib
from fatcat_scholar.api_entities import entity_to_dict
from fatcat_scholar.biblio_hacks import doi_link_domain


class DocType(str, Enum):
    work = "work"
    sim_page = "sim_page"


class IntermediateBundle(BaseModel):
    doc_type: DocType
    releases: List[ReleaseEntity]
    biblio_release_ident: Optional[str]
    grobid_fulltext: Optional[Dict[str, Any]]
    pdftotext_fulltext: Optional[Dict[str, Any]]
    pdf_meta: Optional[Dict[str, Any]]
    html_fulltext: Optional[Dict[str, Any]]
    sim_fulltext: Optional[Dict[str, Any]]
    fetched: Optional[datetime.datetime]

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {
            ReleaseEntity: lambda re: entity_to_dict(re),
            datetime.datetime: lambda dt: dt.isoformat(),
        }


class AccessType(str, Enum):
    ia_sim = "ia_sim"
    ia_file = "ia_file"
    wayback = "wayback"
    web = "web"
    repository = "repository"
    paywall = "paywall"
    loginwall = "loginwall"
    shadow = "shadow"


class ScholarBiblio(BaseModel):
    release_ident: Optional[str]
    title: Optional[str]
    subtitle: Optional[str]
    original_title: Optional[str]
    release_date: Optional[datetime.date]
    release_year: Optional[int]
    release_type: Optional[str]
    release_stage: Optional[str]
    withdrawn_status: Optional[str]
    lang_code: Optional[str]
    country_code: Optional[str]
    volume: Optional[str]
    volume_int: Optional[int]  # TODO: needed?
    issue: Optional[str]
    issue_int: Optional[int]  # TODO: needed?
    pages: Optional[str]
    first_page: Optional[str]
    first_page_int: Optional[int]  # TODO: needed?
    number: Optional[str]

    doi: Optional[str]
    doi_prefix: Optional[str]
    doi_registrar: Optional[str]
    pmid: Optional[str]
    pmcid: Optional[str]
    isbn13: Optional[str]
    wikidata_qid: Optional[str]
    arxiv_id: Optional[str]
    jstor_id: Optional[str]
    doaj_id: Optional[str]
    dblp_id: Optional[str]
    oai_id: Optional[str]

    license_slug: Optional[str]
    publisher: Optional[str]
    publisher_type: Optional[str]
    container_name: Optional[str]
    container_original_name: Optional[str]
    container_ident: Optional[str]
    container_issnl: Optional[str]
    container_wikidata_qid: Optional[str]
    container_sherpa_color: Optional[str]
    issns: List[str]
    container_type: Optional[str]
    contrib_count: Optional[int]
    contrib_names: List[str]
    affiliations: List[str]

    def doi_link_domain(self, default: str = "doi.org") -> str:
        if not self.doi_prefix:
            return default
        domain = doi_link_domain(
            self.doi_prefix,
            container_name=self.container_name,
            publisher=self.publisher,
        )
        if domain:
            return domain
        else:
            return default

    def citation_str(self, style: str) -> Optional[str]:
        """
        Tries to format this biblio metadata as a citation string. If it fails,
        returns None.

        Urgently, will probably refactor to use a proper citeproc library. Will
        need to do something different about author names at the same time.
        """
        if style == "default":
            val = ", ".join(self.contrib_names)
            if val:
                val += ". "
            if self.title:
                val += f'"{self.title}." '
            if self.container_name:
                val += f" {self.container_name}"
                if self.volume and self.issue:
                    val += f" {self.volume}.{self.issue} "
            if self.release_year:
                val += f" ({self.release_year})"
            if self.pages:
                val += f" {self.pages}"
            return val
        return None


class ScholarFulltext(BaseModel):
    lang_code: Optional[str]
    body: Optional[str]
    acknowledgement: Optional[str]
    annex: Optional[str]
    release_ident: Optional[str]
    file_ident: Optional[str]
    file_sha1: Optional[str]
    file_mimetype: Optional[str]
    size_bytes: Optional[int]
    thumbnail_url: Optional[str]
    access_url: Optional[str]
    access_type: Optional[AccessType]

    def remove_access(self) -> Any:
        """
        Returns a fulltext-indexable copy of self, but with access options and
        file-level details removed
        """
        return ScholarFulltext(
            lang_code=self.lang_code,
            body=self.body,
            acknowledgement=self.acknowledgement,
            annex=self.annex,
            release_ident=self.release_ident,
            thumbnail_url=self.thumbnail_url,
        )


class ScholarRelease(BaseModel):
    ident: Optional[str]
    revision: Optional[str]
    title: str
    release_date: Optional[datetime.date]
    release_year: Optional[int]
    release_type: Optional[str]
    release_stage: Optional[str]
    withdrawn_status: Optional[str]

    doi: Optional[str]
    doi_prefix: Optional[str]
    doi_registrar: Optional[str]
    pmid: Optional[str]
    pmcid: Optional[str]
    isbn13: Optional[str]
    wikidata_qid: Optional[str]
    arxiv_id: Optional[str]
    jstor_id: Optional[str]
    doaj_id: Optional[str]
    dblp_id: Optional[str]
    oai_id: Optional[str]

    license_slug: Optional[str]
    container_name: Optional[str]
    container_ident: Optional[str]
    container_issnl: Optional[str]
    container_type: Optional[str]


class ScholarSim(BaseModel):
    issue_item: str
    pub_collection: str
    sim_pubid: str
    first_page: Optional[str]


class ScholarAbstract(BaseModel):
    body: str
    lang_code: Optional[str]


class ScholarAccess(BaseModel):
    access_type: AccessType
    access_url: str
    mimetype: Optional[str]
    file_ident: Optional[str]
    release_ident: Optional[str]


class ScholarDoc(BaseModel):
    key: str
    doc_type: str  # enum: work or page
    doc_index_ts: datetime.datetime
    collapse_key: str
    work_ident: Optional[str]
    tags: List[str] = []

    biblio: ScholarBiblio
    fulltext: Optional[ScholarFulltext]
    ia_sim: Optional[ScholarSim]
    abstracts: List[ScholarAbstract]
    releases: List[ScholarRelease]
    access: List[ScholarAccess]


class RefBiblio(BaseModel):
    unstructured: Optional[str]
    title: Optional[str]
    subtitle: Optional[str]
    contrib_raw_names: Optional[List[str]]
    year: Optional[int]
    container_name: Optional[str]
    publisher: Optional[str]
    volume: Optional[str]
    issue: Optional[str]
    pages: Optional[str]
    doi: Optional[str]
    pmid: Optional[str]
    pmcid: Optional[str]
    arxiv_id: Optional[str]
    isbn13: Optional[str]
    url: Optional[str]


class RefStructured(BaseModel):
    biblio: RefBiblio
    release_ident: Optional[str]
    work_ident: Optional[str]
    release_year: Optional[int]
    index: Optional[int]
    key: Optional[str]
    locator: Optional[str]
    target_release_id: Optional[str]
    ref_source: Optional[str]  # grobid, crossref, pubmed, wikipedia, etc


class RefTarget(BaseModel):
    biblio: RefBiblio
    release_ident: Optional[str]
    work_ident: Optional[str]
    release_stage: Optional[str]
    release_type: Optional[str]


def clean_small_int(raw: Optional[str]) -> Optional[int]:
    if not raw or not raw.isdigit():
        return None
    val = int(raw)
    if abs(val) > 30000:
        return None
    return val


def test_clean_small_int() -> None:
    assert clean_small_int("") is None
    assert clean_small_int(None) is None
    assert clean_small_int("asdf") is None
    assert clean_small_int("iiv") is None
    assert clean_small_int("123") == 123
    assert clean_small_int("1200003") is None
    assert clean_small_int("-123") is None
    assert clean_small_int("48844") is None


def doi_split_prefix(doi: str) -> str:
    return doi.split("/")[0]


def release_doi_registrar(release: ReleaseEntity) -> Optional[str]:
    if not release.ext_ids.doi or not release.extra:
        return None
    for registrar in ("crossref", "datacite", "jalc"):
        if registrar in release.extra:
            return registrar
    # TODO: should we default to Crossref?
    return None


def clean_url_conservative(url: Optional[str]) -> Optional[str]:
    """
    Takes a string which is expected to be a URL, and does some light cleanups.
    If the string looks messy, passes it through anyways for downstream
    processing.

    TODO: attempt URL decoding
    """
    if not url:
        return None
    if url.startswith("<"):
        url = url[1:]
    if ">" in url:
        url = url.split(">")[0]
    return url


def test_clean_url_conservative() -> None:
    assert clean_url_conservative("") is None
    assert clean_url_conservative(None) is None
    assert (
        clean_url_conservative("<http://en.wikipedia.org/wiki/Rumpelstiltskin>")
        == "http://en.wikipedia.org/wiki/Rumpelstiltskin"
    )
    assert (
        clean_url_conservative("<http://en.wikipedia.org/wiki/Baiji>.Acessoem")
        == "http://en.wikipedia.org/wiki/Baiji"
    )
    assert (
        clean_url_conservative("Available:en.m.wikipedia.org/wiki/Jigawa_State")
        == "Available:en.m.wikipedia.org/wiki/Jigawa_State"
    )


UNWANTED_ABSTRACT_PREFIXES = [
    # roughly sort this long to short
    "Abstract No Abstract ",
    "Publisher Summary ",
    "Abstract ",
    "ABSTRACT ",
    "Summary ",
    "Background: ",
    "Background ",
    "N/a.",
    "No abstract.",
    "Introduction: ",
    "ACKNOWLEDGEMENTS ",
    "a b s t r a c t ",
]

UNWANTED_SHORT_STRINGS = [
    "&na",
    "n/a",
]


def clean_str(raw: Optional[str], strip_trailing_period: bool = False) -> Optional[str]:
    """
    Takes a str and "cleans" it. Intended to be usable with short strings
    (names, titles) in any language. See scrub_text(), which extends this
    function for paragraph length and longer text fields.
    """
    if not raw:
        return None

    text = ftfy.fix_text(raw)

    # remove HTML tags
    try:
        # TODO: work_h4ufpvlh3rcefacajni7sdndwa as a regression test
        # TODO: consider w3clib "remove tags" as an alternative
        clean_text = BeautifulSoup(text, "html.parser").get_text()
        text = clean_text
    except UnboundLocalError:
        # TODO: passing through raw string; what should behavior actually be?
        pass

    # TODO: for performance, compile these as globals?
    # replaces whitespace with single space
    text = re.sub(r"\s+", " ", text).strip()

    # TODO: shouldn't HTML be parsing these out?
    text = text.replace("<em>", "").replace("</em>", "")

    text = text.strip()

    if strip_trailing_period and text.endswith("."):
        text = text[:-1]

    if text.lower() in UNWANTED_SHORT_STRINGS:
        return None

    if not text:
        return None
    return text


def scrub_text(raw: str, mimetype: str = None) -> Optional[str]:
    """
    This function takes a mimetype-hinted string and tries to reduce it to a
    simple token-and-punctuation scheme with any and all markup removed. Eg,
    HTML tags, JATS XML tags, LaTeX, whatever.

    Like clean_str(), but more aggressive about some punctuation, and intended
    for text fields (like abstracts), not just short strings.

    The output should be clean and "HTML safe" (though should still be escaped
    in HTML to get entity encoding correct).

    TODO: not using mimetype hint for latex yet
    """
    text = clean_str(raw)
    if not text:
        return None

    # TODO: for performance, compile these as globals?
    # Three regexes below adapted from Blendle cleaner.py
    # https://github.com/blendle/research-summarization/blob/master/enrichers/cleaner.py#L29
    text = re.sub(r"…", "...", text)
    text = re.sub(r"[`‘’‛⸂⸃⸌⸍⸜⸝]", "'", text)
    text = re.sub(r"[„“]|(\'\')|(,,)", '"', text)

    # hack to remove abstract prefixes
    for prefix in UNWANTED_ABSTRACT_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break

    # single word? not "text". eg, random URLs
    if len(text.split()) <= 1:
        return None

    if not text:
        return None
    return text


def contrib_name(contrib: ReleaseContrib) -> str:
    # TODO: support more cultural normals for name presentation
    if contrib.raw_name:
        return contrib.raw_name
    elif contrib.given_name and contrib.surname:
        return f"{contrib.given_name} {contrib.surname}"
    elif contrib.surname:
        return contrib.surname
    else:
        return contrib.given_name


def contrib_affiliation(contrib: ReleaseContrib) -> Optional[str]:
    # TODO
    return None


def es_abstracts_from_grobid(tei_dict: dict) -> List[ScholarAbstract]:

    if tei_dict.get("abstract"):
        body = scrub_text(tei_dict["abstract"])
        if body:
            return [ScholarAbstract(lang_code=tei_dict.get("lang"), body=body)]
    return []


def es_abstracts_from_release(release: ReleaseEntity) -> List[ScholarAbstract]:

    d = dict()
    for abst in release.abstracts:
        if abst.lang not in d:
            body = scrub_text(abst.content)
            if body:
                d[abst.lang] = ScholarAbstract(
                    lang_code=abst.lang, body=scrub_text(abst.content)
                )
    return list(d.values())


def es_biblio_from_release(release: ReleaseEntity) -> ScholarBiblio:

    container_name = release.extra and release.extra.get("container_name")
    container_sherpa_color = None

    if release.container:
        publisher = release.publisher or release.container.publisher
        container_name = container_name or release.container.name
        container_ident = release.container.ident
        container_type = release.container.container_type
        container_issnl = release.container.issnl
        issns = []
        if container_issnl:
            issns.append(container_issnl)
        publisher_type = None
        container_original_name = None
        if release.container.extra:
            publisher_type = release.container.extra.get("publisher_type")
            container_original_name = release.container.extra.get("original_name")
            container_sherpa_color = release.container.extra.get(
                "sherpa_romeo", {}
            ).get("color")
            if release.container.extra.get("issne"):
                issns.append(release.container.extra["issne"])
            if release.container.extra.get("issnp"):
                issns.append(release.container.extra["issnp"])
        issns = list(set(issns))
    else:
        publisher_type = None
        publisher = release.publisher
        container_original_name = None
        container_ident = None
        container_type = None
        container_issnl = None
        issns = []

    first_page: Optional[str] = None
    if release.pages:
        first_page = release.pages.split("-")[0]

    ret = ScholarBiblio(
        release_ident=release.ident,
        title=clean_str(release.title, strip_trailing_period=True),
        subtitle=clean_str(release.subtitle, strip_trailing_period=True),
        original_title=clean_str(release.original_title, strip_trailing_period=True),
        release_date=release.release_date,
        release_year=release.release_year,
        release_type=release.release_type,
        release_stage=release.release_stage,
        withdrawn_status=release.withdrawn_status,
        lang_code=release.language,
        country_code=release.extra and release.extra.get("country"),
        volume=release.volume,
        volume_int=clean_small_int(release.volume),
        issue=release.issue,
        issue_int=clean_small_int(release.issue),
        pages=release.pages,
        first_page=first_page,
        first_page_int=clean_small_int(first_page),
        number=release.number,
        doi=release.ext_ids.doi,
        doi_prefix=release.ext_ids.doi and doi_split_prefix(release.ext_ids.doi),
        doi_registrar=release_doi_registrar(release),
        pmid=release.ext_ids.pmid,
        pmcid=release.ext_ids.pmcid,
        isbn13=release.ext_ids.isbn13,
        wikidata_qid=release.ext_ids.wikidata_qid,
        arxiv_id=release.ext_ids.arxiv,
        jstor_id=release.ext_ids.jstor,
        doaj_id=release.ext_ids.doaj,
        dblp_id=release.ext_ids.dblp,
        oai_id=release.ext_ids.oai,
        license_slug=release.license_slug,
        publisher=publisher,
        publisher_type=publisher_type,
        container_name=clean_str(container_name),
        container_original_name=container_original_name,
        container_ident=container_ident,
        container_type=container_type,
        container_issnl=container_issnl,
        container_sherpa_color=container_sherpa_color,
        issns=issns,
        # TODO; these filters sort of meh. refactor to be above?
        contrib_names=list(
            filter(
                lambda x: bool(x),
                [clean_str(contrib_name(c)) for c in release.contribs],
            )
        ),
        contrib_count=len([c for c in release.contribs if c.index]),
        affiliations=list(
            filter(
                lambda x: bool(x),
                [
                    clean_str(contrib_affiliation(c))
                    for c in release.contribs
                    if c.index
                ],
            )
        ),
    )
    return ret


def es_release_from_release(release: ReleaseEntity) -> ScholarRelease:

    if release.container:
        container_name = release.container.name
        container_ident = release.container.ident
        container_issnl = release.container.issnl
        container_type = release.container.container_type
    else:
        container_name = release.extra and release.extra.get("container_name")
        container_ident = None
        container_issnl = None
        container_type = None

    ret = ScholarRelease(
        ident=release.ident,
        revision=release.revision,
        title=release.title,
        release_date=release.release_date,
        release_year=release.release_year,
        release_type=release.release_type,
        release_stage=release.release_stage,
        withdrawn_status=release.withdrawn_status,
        doi=release.ext_ids.doi,
        doi_prefix=release.ext_ids.doi and doi_split_prefix(release.ext_ids.doi),
        doi_registrar=release_doi_registrar(release),
        pmid=release.ext_ids.pmid,
        pmcid=release.ext_ids.pmcid,
        isbn13=release.ext_ids.isbn13,
        wikidata_qid=release.ext_ids.wikidata_qid,
        arxiv_id=release.ext_ids.arxiv,
        jstor_id=release.ext_ids.jstor,
        doaj_id=release.ext_ids.doaj,
        dblp_id=release.ext_ids.dblp,
        oai_id=release.ext_ids.oai,
        license_slug=release.license_slug,
        container_name=container_name,
        container_ident=container_ident,
        container_issnl=container_issnl,
        container_type=container_type,
    )
    return ret
