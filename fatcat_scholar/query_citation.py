"""
This file contains helpers to fuzzy match a raw citation string:

- try to parse it with GROBID into structured form
- transform the GROBID XML response to a simple dict/struct
- run fuzzycat lookup

Note that this chain hits several external services, and should be wrapped in a
timeout and try/except! In the future, perhaps should be async so it can run in
parallel with "regular" query?
"""

import io
import sys
from typing import Optional, Any, Tuple
import xml.etree.ElementTree as ET

import requests
from fatcat_openapi_client import ReleaseEntity, ReleaseExtIds, ReleaseContrib
from fatcat_scholar.api_entities import entity_to_dict
from fuzzycat.matching import match_release_fuzzy
import fuzzycat.common
import fuzzycat.verify

from fatcat_scholar.grobid2json import biblio_info


def grobid_process_citation(
    raw: str, grobid_host: str = "https://grobid.qa.fatcat.wiki", timeout: float = 10.0
) -> Optional[str]:
    try:
        grobid_response = requests.post(
            grobid_host + "/api/processCitation",
            data={"citations": raw, "consolidateCitations": 0},
            timeout=timeout,
        )
    except requests.Timeout:
        print("GROBID request (HTTP POST) timeout", file=sys.stderr)
        return None
    if grobid_response.status_code != 200:
        print(f"GROBID request (HTTP POST) failed: {grobid_response}", file=sys.stderr)
        return None
    return grobid_response.text


def transform_grobid(raw_xml: str) -> Optional[dict]:
    # first, remove any xmlns stuff
    raw_xml = raw_xml.replace('xmlns="http://www.tei-c.org/ns/1.0"', "")
    tree = ET.parse(io.StringIO(raw_xml))
    root = tree.getroot()
    ref = biblio_info(root, ns="")
    if not any(ref.values()):
        return None
    return ref


def ref_to_release(ref: dict) -> ReleaseEntity:
    contribs = []
    for author in ref.get("authors") or []:
        contribs.append(
            ReleaseContrib(
                raw_name=author.get("name"),
                given_name=author.get("given_name"),
                surname=author.get("surname"),
            )
        )
    release = ReleaseEntity(
        title=ref.get("title"),
        contribs=contribs,
        volume=ref.get("volume"),
        issue=ref.get("issue"),
        pages=ref.get("pages"),
        ext_ids=ReleaseExtIds(
            doi=ref.get("doi"),
            pmid=ref.get("pmid"),
            pmcid=ref.get("pmcid"),
            arxiv=ref.get("arxiv_id"),
        ),
    )
    if ref.get("journal"):
        release.extra = {"container_name": ref.get("journal")}
    if ref.get("date"):
        if len(ref["date"]) == 4 and ref["date"].isdigit():
            release.release_year = int(ref["date"])
    return release


def fuzzy_match(
    release: ReleaseEntity, es_client: Any, api_client: Any, timeout: float = 10.0
) -> Optional[Tuple[str, str, ReleaseEntity]]:
    """
    This helper function uses fuzzycat (and elasticsearch) to look for
    existing release entities with similar metadata.

    Returns None if there was no match of any kind, or a single tuple
    (status: str, reason: str, existing: ReleaseEntity) if there was a match.

    Status string is one of the fuzzycat.common.Status, with "strongest
    match" in this sorted order:

    - EXACT
    - STRONG
    - WEAK
    - AMBIGUOUS

    Eg, if there is any EXACT match that is always returned; an AMBIGIOUS
    result is only returned if all the candidate matches were ambiguous.

    TODO: actually do something with timeout
    """

    # this map used to establish priority order of verified matches
    STATUS_SORT = {
        fuzzycat.common.Status.TODO: 0,
        fuzzycat.common.Status.EXACT: 10,
        fuzzycat.common.Status.STRONG: 20,
        fuzzycat.common.Status.WEAK: 30,
        fuzzycat.common.Status.AMBIGUOUS: 40,
        fuzzycat.common.Status.DIFFERENT: 60,
    }

    # TODO: the size here is a first guess; what should it really be?
    candidates = match_release_fuzzy(release, size=10, es=es_client)
    if not candidates:
        return None

    release_dict = entity_to_dict(release, api_client=api_client.api_client)
    verified = [
        (
            fuzzycat.verify.verify(
                release_dict, entity_to_dict(c, api_client=api_client.api_client)
            ),
            c,
        )
        for c in candidates
    ]

    # chose the "closest" match
    closest = sorted(verified, key=lambda v: STATUS_SORT[v[0].status])[0]
    if closest[0].status == fuzzycat.common.Status.DIFFERENT:
        return None
    elif closest[0].status == fuzzycat.common.Status.TODO:
        raise NotImplementedError("fuzzycat verify hit a Status.TODO")
    else:
        return (closest[0].status.name, closest[0].reason.value, closest[1])


def try_fuzzy_match(
    citation: str, grobid_host: str, es_client: Any, fatcat_api_client: Any
) -> Optional[str]:
    """
    All-in-one helper method
    """
    resp = grobid_process_citation(citation, grobid_host=grobid_host, timeout=3.0)
    if not resp:
        return None
    ref = transform_grobid(resp)
    if not ref:
        return None
    release = ref_to_release(ref)

    matches = fuzzy_match(
        release, es_client=es_client, api_client=fatcat_api_client, timeout=3.0
    )
    if not matches or matches[0] not in ("EXACT", "STRONG", "WEAK"):
        return None
    return f"work_{matches[2].work_id}"


if __name__ == "__main__":
    """
    Demo showing how to integrate the above functions together.
    """
    import os
    import elasticsearch
    import fatcat_openapi_client

    citation = sys.argv[1]
    print("Sending to GROBID...")
    resp = grobid_process_citation(citation)
    print(resp)
    if not resp:
        sys.exit(0)
    ref = transform_grobid(resp)
    print(ref)
    if not ref:
        sys.exit(0)
    release = ref_to_release(ref)
    print(release)

    es_backend = os.environ.get(
        "ELASTICSEARCH_FATCAT_BASE", "https://search.fatcat.wiki"
    )
    es_client = elasticsearch.Elasticsearch(es_backend)
    api_conf = fatcat_openapi_client.Configuration()
    api_conf.host = os.environ.get("FATCAT_API_HOST", "https://api.fatcat.wiki/v0")
    api_client = fatcat_openapi_client.DefaultApi(
        fatcat_openapi_client.ApiClient(api_conf)
    )
    matches = fuzzy_match(release, es_client=es_client, api_client=api_client)
    print(matches)
    if not matches or matches[0] not in ("EXACT", "STRONG", "WEAK"):
        sys.exit(0)
    print(matches[2].work_id)
