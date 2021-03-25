import sys
import json
import argparse
import datetime
import xml.etree.ElementTree
import xml.etree.ElementTree as ET
from typing import List, Dict, Optional, Any, Sequence

import sentry_sdk
from fatcat_openapi_client import ReleaseEntity, FileEntity, WebcaptureEntity

from fatcat_scholar.api_entities import entity_from_json
from fatcat_scholar.config import settings, GIT_REVISION
from fatcat_scholar.grobid2json import teixml2json
from fatcat_scholar.schema import (
    AccessType,
    clean_small_int,
    clean_url_conservative,
    DocType,
    es_abstracts_from_grobid,
    es_abstracts_from_release,
    es_biblio_from_release,
    es_release_from_release,
    IntermediateBundle,
    RefBiblio,
    RefStructured,
    ScholarAccess,
    ScholarAbstract,
    ScholarBiblio,
    ScholarDoc,
    ScholarFulltext,
    ScholarSim
)

MAX_BODY_CHARS = 512 * 1024


def es_fulltext_from_sim(sim: Dict[str, Any]) -> Optional[ScholarFulltext]:
    if not sim["page_texts"]:
        return None
    first_page = sim["page_texts"][0]["page_num"]
    issue_item = sim["issue_item"]
    body = "\n".join([p["raw_text"] for p in sim["page_texts"]])
    if body and len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS]
    return ScholarFulltext(
        lang_code=None,  # TODO: pub/issue metadata? or langdetect?
        body=body,
        # acknowledgement=None,
        # annex=None,
        release_ident=sim.get("release_ident"),
        # file_ident=None,
        # file_sha1=None,
        # file_mimetype=None,
        # size_bytes=None,
        thumbnail_url=f"https://archive.org/serve/{issue_item}/__ia_thumb.jpg",
        access_url=f"https://archive.org/details/{issue_item}/page/{first_page}",
        access_type=AccessType.ia_sim,
    )


def es_sim_from_sim(sim: Dict[str, Any]) -> ScholarSim:
    first_page = None
    if sim["page_texts"]:
        first_page = sim["page_texts"][0]["page_num"]
    return ScholarSim(
        issue_item=sim["issue_item"],
        pub_collection=sim["pub_item_metadata"]["metadata"]["identifier"],
        sim_pubid=sim["issue_item_metadata"]["metadata"]["sim_pubid"],
        first_page=first_page,
    )


SIM_RELEASE_TYPE_MAP = {
    "Scholarly Journals": "article-journal",
    "Trade Journals": "article-magazine",
    "Historical Journals": "article",
    "Magazines": "article-magazine",
    "Government Documents": "article",
    "Law Journals": "article",
    # TODO: more categories?
}
SIM_LANG_MAP = {
    "English": "en",
    "German": "de",
    "Italian": "it",
    "French": "fr",
    "Afrikaans": "af",
    "Spanish": "es",
    "Portuguese": "pt",
    "Polish": "pl",
    # TODO: more languages in SIM corpus
}
SIM_COUNTRY_MAP = {
    "United States": "us",
    "Germany": "de",
    "Netherlands": "nl",
    "United Kingdom": "",
    "Canada": "ca",
    "Switzerland": "ch",
    "South Africa": "za",
    "Japan": "jp",
    "France": "fr",
    "India": "in",
    "Chile": "cl",
    "Brazil": "br",
    "Australia": "au",
    "Argentina": "ar",
    "Uganda": "ug",
    "Thailand": "th",
    "Puerto Rico": "pr",
    "Poland": "pl",
    "Philippines": "ph",
    "Mexico": "mx",
    "Jamaica": "jm",
    "Italy": "it",
    "Ireland": "ie",
    "Finland": "fi",
    "Bulgaria": "bg",
    # TODO: more countries in SIM corpus
}


def es_biblio_from_sim(sim: Dict[str, Any]) -> ScholarBiblio:

    issue_meta = sim["issue_item_metadata"]["metadata"]
    pub_meta = sim["pub_item_metadata"]["metadata"]

    first_page = None
    if sim["page_texts"]:
        first_page = sim["page_texts"][0]["page_num"] or None
    first_page_int = clean_small_int(first_page)

    container_name = sim["pub_item_metadata"]["metadata"]["title"]

    # can't remember what this hack is for...
    last_word = container_name.split()[-1]
    if len(last_word) == 9 and last_word[4] == "-":
        container_name = container_name[:-10]

    issns = []
    raw_issn = issue_meta.get("issn")
    if raw_issn and len(raw_issn) == 9:
        issns.append(raw_issn)

    volume = issue_meta.get("volume")
    volume_int = clean_small_int(volume)
    issue = issue_meta.get("issue")
    issue_int = clean_small_int(issue)

    date = issue_meta.get("date")
    release_year = None
    if date and len(date) > 4 and date[:4].isdigit():
        release_year = int(date[:4])

    release_date = None
    if len(date) == len("2000-01-01"):
        try:
            datetime.date.fromisoformat(date)
            release_date = date
        except ValueError:
            pass

    if release_year and abs(release_year) > 2050:
        release_year = None

    language = issue_meta.get("language") or pub_meta.get("language")
    if isinstance(language, list):
        language = language[0]
    lang_code = SIM_LANG_MAP.get(language)

    return ScholarBiblio(
        # release_ident=release.ident,
        title=None,
        # subtitle=None,
        # original_title=release.original_title,
        release_date=release_date,
        release_year=release_year,
        release_type=SIM_RELEASE_TYPE_MAP.get(issue_meta.get("pub_type"))
        or SIM_RELEASE_TYPE_MAP.get(pub_meta.get("pub_type")),
        release_stage="published",  # as a default
        # withdrawn_status=release.withdrawn_status,
        lang_code=lang_code,
        country_code=SIM_COUNTRY_MAP.get(pub_meta.get("country")),
        volume=volume,
        volume_int=volume_int,
        issue=issue,
        issue_int=issue_int,
        pages=sim.get("pages"),
        first_page=first_page,
        first_page_int=first_page_int,
        # number=None,
        # no external identifiers
        # license_slug=release.license_slug,
        publisher=issue_meta.get("publisher") or pub_meta.get("publisher"),
        container_name=container_name,
        container_original_name=None,
        container_ident=None,  # TODO
        container_type=None,  # TODO
        container_issnl=None,  # TODO
        # container_sherpa_color
        issns=issns,
        # no contrib/affiliation info
        contrib_names=[],
        affiliations=[],
    )


def _add_file_release_meta(
    fulltext: ScholarFulltext,
    pdf_meta: Optional[dict],
    re: ReleaseEntity,
    fe: FileEntity,
) -> ScholarFulltext:
    best_url = None
    best_url_type = None
    for url in fe.urls:
        best_url = url.url
        best_url_type = AccessType.web
        if "//archive.org/" in url.url:
            best_url_type = AccessType.ia_file
            break
        elif "//web.archive.org/" in url.url:
            best_url_type = AccessType.wayback
            break
        if url.rel == "repository":
            best_url_type = AccessType.repository
        # TODO: more file-to-access logic

    fulltext.release_ident = re.ident
    fulltext.file_ident = fe.ident
    fulltext.file_sha1 = fe.sha1
    fulltext.file_mimetype = fe.mimetype
    fulltext.size_bytes = fe.size
    fulltext.access_url = best_url
    fulltext.access_type = best_url_type
    if pdf_meta is not None and pdf_meta["pdf_meta"].get("has_page0_thumbnail"):
        # eg: https://blobs.fatcat.wiki/thumbnail/pdf/32/29/322909fe57cef73b10a166996a4528d337026d16.180px.jpg
        fulltext.thumbnail_url = f"{ settings.THUMBNAIL_URL_PREFIX }{ fe.sha1[0:2] }/{ fe.sha1[2:4] }/{ fe.sha1 }.180px.jpg"
    return fulltext


def es_fulltext_from_grobid(
    tei_dict: dict, pdf_meta: Optional[dict], re: ReleaseEntity, fe: FileEntity
) -> Optional[ScholarFulltext]:
    if not tei_dict.get("body"):
        return None
    body = tei_dict.get("body")
    if body and len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS]
    ret = ScholarFulltext(
        lang_code=tei_dict.get("lang"),
        body=body,
        acknowledgement=tei_dict.get("acknowledgement"),
        annex=tei_dict.get("annex"),
    )
    return _add_file_release_meta(ret, pdf_meta, re, fe)


def es_fulltext_from_pdftotext(
    raw_text: str, pdf_meta: Optional[dict], re: ReleaseEntity, fe: FileEntity
) -> Optional[ScholarFulltext]:

    if raw_text and len(raw_text) > MAX_BODY_CHARS:
        raw_text = raw_text[:MAX_BODY_CHARS]
    ret = ScholarFulltext(
        lang_code=re.language, body=raw_text, acknowledgement=None, annex=None,
    )
    return _add_file_release_meta(ret, pdf_meta, re, fe)


def es_fulltext_from_html(
    html_fulltext: Dict[str, Any], re: ReleaseEntity, wc: WebcaptureEntity,
) -> Optional[ScholarFulltext]:

    if not wc.archive_urls or not html_fulltext.get("tei_xml"):
        return None

    ns = {"tei": "http://www.tei-c.org/ns/1.0"}
    tree = ET.fromstring(html_fulltext["tei_xml"])
    body = tree.find(".//tei:body", ns)
    if body:
        raw_text = " ".join(body.itertext())
        if raw_text and len(raw_text) > MAX_BODY_CHARS:
            raw_text = raw_text[:MAX_BODY_CHARS]
    else:
        return None

    ret = ScholarFulltext(
        lang_code=re.language,
        body=raw_text,
        acknowledgement=None,
        annex=None,
        release_ident=re.ident,
        # webcapture_ident=wc.ident,
        file_sha1=html_fulltext.get("html_meta", {}).get("sha1hex"),
        file_mimetype="text/html",
        # size_bytess
        access_url=wc.archive_urls[0].url,
        access_type=AccessType.wayback,
    )
    return ret


def biblio_metadata_hacks(biblio: ScholarBiblio) -> ScholarBiblio:  # noqa: C901
    """
    This function does platform/publisher specific metadata hacks.

    Really these should be updated in the fatcat catalog directly, but in the
    short term want to work around some large-ish transforms for our prototype
    index.

    This function is long, but simple in structure, so not likely to refactor
    into smaller functions.
    """

    # valid year
    if biblio.release_year and (
        biblio.release_year > 2025 or biblio.release_year < 1500
    ):
        biblio.release_year = None
        biblio.release_date = None

    # figshare
    if biblio.doi_prefix in ("10.6084", "10.25384"):
        if not biblio.container_name:
            biblio.container_name = "Figshare"

    # zenodo
    if biblio.doi_prefix == "10.5281":
        if not biblio.container_name:
            biblio.container_name = "Zenodo"

    # biorxiv/medrxiv
    # NOTE: there is a further hack that determines which of biorxiv/medrxiv
    # based on access URL
    if biblio.doi_prefix == "10.1101":
        if not biblio.container_name:
            biblio.container_name = "biorxiv/medrxiv"
        if not biblio.release_stage:
            biblio.release_stage = "submitted"
        if biblio.release_type == "post":
            biblio.release_type = "article"

    # arxiv
    if biblio.arxiv_id and not (biblio.doi or biblio.pmid):
        if not biblio.container_name:
            biblio.container_name = "arXiv"
        if biblio.release_type in (None, "report", "post"):
            biblio.release_type = "article"

    # IEEE
    if biblio.doi_prefix == "10.1109":
        if (
            not biblio.release_stage
            and biblio.container_name
            and (
                "IEEE" in biblio.container_name
                or "Conference" in biblio.container_name
                or "Proceedings" in biblio.container_name
                or biblio.release_type == "paper-conference"
            )
        ):
            biblio.release_stage = "published"

    # ACM
    if biblio.doi_prefix == "10.1145":
        if (
            not biblio.release_stage
            and biblio.container_name
            and (
                "ACM" in biblio.container_name
                or "Conference" in biblio.container_name
                or "Proceedings" in biblio.container_name
            )
        ):
            biblio.release_stage = "published"

    # IOP, ACM, IEEE, AIP, World Scientific (large conference publishers)
    if biblio.doi_prefix in ("10.1145", "10.1109", "10.1117", "10.1063", "10.1142"):
        if not biblio.release_stage and biblio.release_type == "paper-conference":
            biblio.release_stage = "published"

    # F1000
    if biblio.doi_prefix == "10.3510":
        if biblio.title and biblio.title.startswith("Faculty of 1000 evaluation for"):
            biblio.release_type = "peer_review"
            biblio.release_stage = "published"

    # protocols.io
    if biblio.doi_prefix == "10.17504":
        if not biblio.release_stage:
            biblio.release_stage = "published"

    return biblio


def generate_tags(
    biblio: ScholarBiblio, primary_release: Optional[ReleaseEntity]
) -> List[str]:

    tags = []

    # tags
    if biblio.license_slug and biblio.license_slug.lower().startswith("cc-"):
        tags.append("oa")
    if primary_release and primary_release.container:
        container = primary_release.container
        if container.extra:
            if container.extra.get("doaj") or biblio.doaj_id:
                tags.append("doaj")
                tags.append("oa")
            if container.extra.get("road"):
                tags.append("road")
                tags.append("oa")
            if container.extra.get("szczepanski"):
                tags.append("szczepanski")
                if biblio.publisher_type not in ["big5"]:
                    # szczepanski not accurate OA flag for some large publishers (eg, Springer)
                    tags.append("oa")
            if container.extra.get("ia", {}).get("longtail_oa"):
                tags.append("longtail")
                tags.append("oa")
            if container.extra.get("default_license", "").lower().startswith("cc-"):
                tags.append("oa")
            if container.extra.get("platform"):
                # scielo, ojs, wordpress, etc
                tags.append(container.extra["platform"].lower())
    if biblio.doi_prefix == "10.2307" or biblio.jstor_id:
        tags.append("jstor")
    return list(set(tags))


def check_exclude_web(biblio: ScholarBiblio) -> bool:
    """
    Returns a flag that fulltext web archive options to a work should not be
    linked to from web interface
    """
    if biblio.release_year and biblio.release_year <= 1925:
        return False
    if (
        biblio.container_ident
        and biblio.container_ident in settings.EXCLUDE_WEB_CONTAINER_IDENTS
    ):
        return True
    if biblio.publisher:
        for pub in settings.EXCLUDE_WEB_PUBLISHERS:
            if pub in biblio.publisher.lower():
                return True
    if biblio.license_slug and biblio.license_slug.startswith("cc-"):
        return False
    if biblio.pmcid:
        return False
    if biblio.container_sherpa_color and biblio.container_sherpa_color == "white":
        return True
    return False


def transform_heavy(heavy: IntermediateBundle) -> Optional[ScholarDoc]:

    tags: List[str] = []
    work_ident: Optional[str] = None
    sim_issue: Optional[str] = None
    abstracts: List[ScholarAbstract] = []
    fulltext: Optional[ScholarFulltext] = None
    primary_release: Optional[ReleaseEntity] = None
    exclude_web_fulltext: bool = False

    ia_sim: Optional[ScholarSim] = None
    if heavy.sim_fulltext is not None:
        ia_sim = es_sim_from_sim(heavy.sim_fulltext)
        fulltext = es_fulltext_from_sim(heavy.sim_fulltext)

    if heavy.doc_type == DocType.sim_page:
        assert ia_sim is not None
        assert heavy.sim_fulltext is not None
        if not ia_sim.first_page or not ia_sim.issue_item:
            # can't create a valid key if we don't have these fields, so shouldn't index
            return None
        key = f"page_{ia_sim.issue_item}_{ia_sim.first_page}"
        sim_issue = ia_sim.issue_item
        biblio = es_biblio_from_sim(heavy.sim_fulltext)
        # fulltext extracted from heavy.sim_fulltext above
    elif heavy.doc_type == DocType.work:
        work_ident = heavy.releases[0].work_id
        key = f"work_{work_ident}"
        assert heavy.biblio_release_ident
        primary_release = [
            r for r in heavy.releases if r.ident == heavy.biblio_release_ident
        ][0]
        biblio = es_biblio_from_release(primary_release)
        biblio = biblio_metadata_hacks(biblio)
        exclude_web_fulltext = check_exclude_web(biblio)
        abstracts = es_abstracts_from_release(primary_release)

        # if no abstract from primary_release, try all the other releases
        for release in heavy.releases:
            if not abstracts:
                abstracts = es_abstracts_from_release(release)
    else:
        raise NotImplementedError(f"doc_type: {heavy.doc_type}")

    if heavy.grobid_fulltext:
        fulltext_release = [
            r
            for r in heavy.releases
            if r.ident == heavy.grobid_fulltext["release_ident"]
        ][0]
        fulltext_file = [
            f
            for f in fulltext_release.files
            if f.ident == heavy.grobid_fulltext["file_ident"]
        ][0]
        try:
            tei_dict: Optional[dict] = teixml2json(heavy.grobid_fulltext["tei_xml"])
        except xml.etree.ElementTree.ParseError:
            tei_dict = None
        if tei_dict:
            if not abstracts:
                abstracts = es_abstracts_from_grobid(tei_dict)
            grobid_fulltext = es_fulltext_from_grobid(
                tei_dict, heavy.pdf_meta, fulltext_release, fulltext_file
            )
            if exclude_web_fulltext and grobid_fulltext:
                if not fulltext:
                    # include only partial fulltext object, with no access
                    fulltext = grobid_fulltext.remove_access()
            else:
                fulltext = grobid_fulltext

    if not fulltext and heavy.pdftotext_fulltext:
        fulltext_release = [
            r
            for r in heavy.releases
            if r.ident == heavy.pdftotext_fulltext["release_ident"]
        ][0]
        fulltext_file = [
            f
            for f in fulltext_release.files
            if f.ident == heavy.pdftotext_fulltext["file_ident"]
        ][0]
        pdftotext_fulltext = es_fulltext_from_pdftotext(
            heavy.pdftotext_fulltext["raw_text"],
            heavy.pdf_meta,
            fulltext_release,
            fulltext_file,
        )
        if exclude_web_fulltext and pdftotext_fulltext:
            fulltext = pdftotext_fulltext.remove_access()
        else:
            fulltext = pdftotext_fulltext

    if not fulltext and heavy.html_fulltext:
        fulltext_release = [
            r for r in heavy.releases if r.ident == heavy.html_fulltext["release_ident"]
        ][0]
        fulltext_webcapture = [
            f
            for f in fulltext_release.webcaptures
            if f.ident == heavy.html_fulltext["webcapture_ident"]
        ][0]
        html_fulltext = es_fulltext_from_html(
            heavy.html_fulltext, fulltext_release, fulltext_webcapture,
        )
        if exclude_web_fulltext and html_fulltext:
            fulltext = html_fulltext.remove_access()
        else:
            fulltext = html_fulltext

    # TODO: additional access list (eg, HTML if only PDF currently)
    access_dict = dict()
    if fulltext and fulltext.access_type:
        access_dict[fulltext.access_type] = ScholarAccess(
            access_type=fulltext.access_type,
            access_url=fulltext.access_url,
            mimetype=fulltext.file_mimetype,
            file_ident=fulltext.file_ident,
            release_ident=fulltext.release_ident,
        )
    if ia_sim and AccessType.ia_sim not in access_dict:
        access_dict[AccessType.ia_sim] = ScholarAccess(
            access_type=AccessType.ia_sim,
            access_url=f"https://archive.org/details/{ia_sim.issue_item}/page/{ia_sim.first_page}",
            # TODO: release_ident
        )

    # TODO: additional abstracts (?)

    tags = generate_tags(biblio, primary_release)

    # biorxiv/medrxiv hacks
    if (
        biblio.doi_prefix == "10.1101"
        and biblio.container_name in (None, "biorxiv/medrxiv")
        and biblio.release_stage != "published"
    ):
        for _, acc in access_dict.items():
            if "://www.medrxiv.org/" in acc.access_url:
                biblio.container_name = "medRxiv"
                if biblio.release_stage is None:
                    biblio.release_stage = "submitted"
            elif "://www.biorxiv.org/" in acc.access_url:
                biblio.container_name = "bioRxiv"
                if biblio.release_stage is None:
                    biblio.release_stage = "submitted"

    return ScholarDoc(
        key=key,
        collapse_key=sim_issue or work_ident,
        doc_type=heavy.doc_type.value,
        doc_index_ts=datetime.datetime.utcnow(),
        work_ident=work_ident,
        tags=tags,
        biblio=biblio,
        fulltext=fulltext,
        ia_sim=ia_sim,
        abstracts=abstracts,
        releases=[es_release_from_release(r) for r in heavy.releases],
        access=list(access_dict.values()),
    )


def refs_from_grobid(release: ReleaseEntity, tei_dict: dict) -> Sequence[RefStructured]:
    output = []
    for ref in tei_dict.get("citations") or []:
        ref_date = ref.get("date") or None
        ref_year: Optional[int] = None
        if ref_date and len(ref_date) >= 4 and ref_date[:4].isdigit():
            ref_year = int(ref_date[:4])
        ref_authors = ref.get("authors") or []
        authors: List[str] = []
        for a in ref_authors:
            if isinstance(a, str):
                authors.append(a)
            elif isinstance(a, dict):
                if a.get("name"):
                    assert isinstance(a["name"], str)
                    authors.append(a["name"])
        output.append(
            RefStructured(
                biblio=RefBiblio(
                    unstructured=ref.get("unstructured"),
                    title=ref.get("title"),
                    # subtitle
                    contrib_raw_names=authors or None,
                    year=ref_year,
                    container_name=ref.get("journal"),
                    publisher=ref.get("publisher"),
                    volume=ref.get("volume"),
                    issue=ref.get("issue"),
                    pages=ref.get("pages"),
                    doi=ref.get("doi"),
                    pmid=ref.get("pmid"),
                    pmcid=ref.get("pmcid"),
                    arxiv_id=ref.get("arxiv_id"),
                    # isbn13: Optional[str]
                    url=clean_url_conservative(ref.get("url")),
                ),
                release_ident=release.ident,
                work_ident=release.work_id,
                release_year=release.release_year,
                index=ref.get("index"),
                key=ref.get("id"),
                locator=None,
                # target_release_id
                ref_source="grobid",
            )
        )
    return output


def refs_from_release_refs(release: ReleaseEntity) -> Sequence[RefStructured]:
    output = []
    for ref in release.refs:
        ref_source = "fatcat"
        if release.extra and release.extra.get("pubmed"):
            ref_source = "pubmed"
        elif release.extra and release.extra.get("crossref"):
            ref_source = "crossref"
        elif release.extra and release.extra.get("datacite"):
            ref_source = "datacite"
        extra = ref.extra or dict()
        authors = extra.get("authors") or []
        authors = [a for a in authors if type(a) == str]
        output.append(
            RefStructured(
                biblio=RefBiblio(
                    unstructured=extra.get("unstructured"),
                    title=ref.title,
                    subtitle=extra.get("subtitle"),
                    contrib_raw_names=authors or None,
                    year=ref.year,
                    container_name=ref.container_name,
                    publisher=extra.get("publisher"),
                    volume=extra.get("volume"),
                    issue=extra.get("issue"),
                    pages=extra.get("pages") or extra.get("page"),
                    doi=extra.get("doi"),
                    pmid=extra.get("pmid"),
                    pmcid=extra.get("pmcid"),
                    arxiv_id=extra.get("arxiv_id"),
                    isbn13=extra.get("isbn13"),
                    url=clean_url_conservative(extra.get("url")),
                ),
                release_ident=release.ident,
                work_ident=release.work_id,
                release_year=release.release_year,
                index=ref.index,
                key=ref.key,
                locator=ref.locator,
                target_release_id=ref.target_release_id,
                ref_source=ref_source,
            )
        )
    return output


def refs_from_heavy(heavy: IntermediateBundle) -> Sequence[RefStructured]:
    """
    Current behavior is to return *both* fatcat refs and GROBID refs if
    available.
    """

    if heavy.doc_type != DocType.work:
        return []

    # first, identify source of refs: fatcat release metadata or GROBID
    assert heavy.biblio_release_ident
    primary_release = [
        r for r in heavy.releases if r.ident == heavy.biblio_release_ident
    ][0]

    refs: List[RefStructured] = []

    if primary_release.refs:
        # TODO: what about other releases?
        refs.extend(refs_from_release_refs(primary_release))

    if heavy.grobid_fulltext:
        fulltext_release = [
            r
            for r in heavy.releases
            if r.ident == heavy.grobid_fulltext["release_ident"]
        ][0]
        tei_dict = teixml2json(heavy.grobid_fulltext["tei_xml"])
        refs.extend(refs_from_grobid(fulltext_release, tei_dict))

    return refs


def run_transform(infile: Sequence) -> None:
    for line in infile:
        obj = json.loads(line)

        heavy = IntermediateBundle(
            doc_type=DocType(obj["doc_type"]),
            releases=[
                entity_from_json(json.dumps(re), ReleaseEntity)
                for re in obj["releases"]
            ],
            biblio_release_ident=obj.get("biblio_release_ident"),
            grobid_fulltext=obj.get("grobid_fulltext"),
            pdftotext_fulltext=obj.get("pdftotext_fulltext"),
            pdf_meta=obj.get("pdf_meta"),
            sim_fulltext=obj.get("sim_fulltext"),
            html_fulltext=obj.get("html_fulltext"),
        )
        es_doc = transform_heavy(heavy)
        if not es_doc:
            continue
        print(es_doc.json(exclude_none=True, sort_keys=True))


def run_refs(infile: Sequence) -> None:
    for line in infile:
        obj = json.loads(line)

        heavy = IntermediateBundle(
            doc_type=DocType(obj["doc_type"]),
            releases=[
                entity_from_json(json.dumps(re), ReleaseEntity)
                for re in obj["releases"]
            ],
            biblio_release_ident=obj.get("biblio_release_ident"),
            grobid_fulltext=obj.get("grobid_fulltext"),
            pdftotext_fulltext=obj.get("pdftotext_fulltext"),
            pdf_meta=obj.get("pdf_meta"),
            sim_fulltext=obj.get("sim_fulltext"),
            html_fulltext=obj.get("html_fulltext"),
        )
        refs = refs_from_heavy(heavy)
        for ref in refs:
            print(ref.json(exclude_none=True, sort_keys=True))


def main() -> None:
    """
    Run this command like:

        python -m fatcat_scholar.transform
    """

    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    subparsers = parser.add_subparsers()

    sub = subparsers.add_parser(
        "run_transform",
        help="takes 'heavy' intermediate, outputs scholar_fulltext ES documents",
    )
    sub.set_defaults(func="run_transform")
    sub.add_argument(
        "json_file",
        help="intermediate globs as JSON-lines",
        nargs="?",
        default=sys.stdin,
        type=argparse.FileType("r"),
    )

    sub = subparsers.add_parser(
        "run_refs", help="extracts references from 'heavy' intermediate"
    )
    sub.set_defaults(func="run_refs")
    sub.add_argument(
        "json_file",
        help="intermediate globs as JSON-lines",
        nargs="?",
        default=sys.stdin,
        type=argparse.FileType("r"),
    )

    args = parser.parse_args()
    if not args.__dict__.get("func"):
        parser.print_help(file=sys.stderr)
        sys.exit(-1)

    # enable sentry exception catching; this helps a lot with debugging bulk
    # transform runs
    if settings.SENTRY_DSN:
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment=settings.SCHOLAR_ENV,
            max_breadcrumbs=10,
            release=GIT_REVISION,
        )

    if args.func == "run_transform":
        run_transform(infile=args.json_file)
    elif args.func == "run_refs":
        run_refs(infile=args.json_file)
    else:
        raise NotImplementedError(args.func)


if __name__ == "__main__":
    main()
