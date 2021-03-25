"""
Helpers to make elasticsearch queries.
"""

import copy
import logging
import datetime
from gettext import gettext
from typing import List, Optional, Any

import elasticsearch
from elasticsearch_dsl import Search, Q
from elasticsearch_dsl.response import Response
import fatcat_openapi_client

# pytype: disable=import-error
from pydantic import BaseModel

# pytype: enable=import-error

from fatcat_scholar.config import settings
from fatcat_scholar.identifiers import clean_doi, clean_pmcid
from fatcat_scholar.schema import ScholarDoc
from fatcat_scholar.query_parse import sniff_citation_query, pre_parse_query
from fatcat_scholar.query_citation import try_fuzzy_match

# i18n note: the use of gettext below doesn't actually do the translation here,
# it just ensures that the strings are caught by babel for translation later


class FulltextQuery(BaseModel):
    q: Optional[str] = None
    limit: Optional[int] = None
    offset: Optional[int] = None
    filter_time: Optional[str] = None
    filter_type: Optional[str] = None
    filter_availability: Optional[str] = None
    sort_order: Optional[str] = None
    collapse_key: Optional[str] = None
    debug: Optional[bool] = False
    time_options: Any = {
        "label": gettext("Release Date"),
        "slug": "filter_time",
        "default": "all_time",
        "list": [
            {"label": gettext("All Time"), "slug": "all_time"},
            {"label": gettext("Past Week"), "slug": "past_week"},
            {"label": gettext("Past Year"), "slug": "past_year"},
            {"label": gettext("Since 2000"), "slug": "since_2000"},
            {"label": gettext("Before 1925"), "slug": "before_1925"},
        ],
    }
    type_options: Any = {
        "label": gettext("Resource Type"),
        "slug": "filter_type",
        "default": "papers",
        "list": [
            {"label": gettext("Papers"), "slug": "papers"},
            {"label": gettext("Reports"), "slug": "reports"},
            {"label": gettext("Datasets"), "slug": "datasets"},
            {"label": gettext("Everything"), "slug": "everything"},
        ],
    }
    availability_options: Any = {
        "label": gettext("Availability"),
        "slug": "filter_availability",
        "default": "fulltext",
        "list": [
            {"label": gettext("Fulltext"), "slug": "fulltext"},
            {"label": gettext("Microfilm"), "slug": "microfilm"},
            {"label": gettext("Open Access"), "slug": "oa"},
            {"label": gettext("Metadata"), "slug": "everything"},
        ],
    }
    sort_options: Any = {
        "label": gettext("Sort Order"),
        "slug": "sort_order",
        "default": "relevancy",
        "list": [
            {"label": gettext("Relevancy"), "slug": "relevancy"},
            {"label": gettext("Recent First"), "slug": "time_desc"},
            {"label": gettext("Oldest First"), "slug": "time_asc"},
        ],
    }


class FulltextHits(BaseModel):
    query_type: str
    count_returned: int
    count_found: int
    offset: int
    limit: int
    deep_page_limit: int
    query_time_ms: int
    query_wall_time_ms: int
    results: List[Any]


# global sync client connection
es_client = elasticsearch.Elasticsearch(settings.ELASTICSEARCH_QUERY_BASE, timeout=25.0)


def transform_es_results(resp: Response) -> List[dict]:
    # convert from ES objects to python dicts
    results = []
    for h in resp:
        r = h._d_
        # print(h.meta._d_)
        r["_highlights"] = []
        if "highlight" in dir(h.meta):
            highlights = h.meta.highlight._d_
            for k in highlights:
                r["_highlights"] += highlights[k]
        r["_collapsed"] = []
        r["_collapsed_count"] = 0
        if "inner_hits" in dir(h.meta):
            if isinstance(h.meta.inner_hits.more_pages.hits.total, int):
                r["_collapsed_count"] = h.meta.inner_hits.more_pages.hits.total - 1
            else:
                r["_collapsed_count"] = (
                    h.meta.inner_hits.more_pages.hits.total["value"] - 1
                )
            for k in h.meta.inner_hits.more_pages:
                if k["key"] != r["key"]:
                    r["_collapsed"].append(k)
        results.append(r)

    for h in results:
        # Handle surrogate strings that elasticsearch returns sometimes,
        # probably due to mangled data processing in some pipeline.
        # "Crimes against Unicode"; production workaround
        for key in h:
            if type(h[key]) is str:
                h[key] = h[key].encode("utf8", "ignore").decode("utf8")
        # ensure collapse_key is a single value, not an array
        if type(h["collapse_key"]) == list:
            h["collapse_key"] = h["collapse_key"][0]
        # add ScholarDoc object as a helper (eg, to call python helpers)
        try:
            h["_obj"] = ScholarDoc.parse_obj(h)
        except Exception:
            pass
    return results


def apply_filters(search: Search, query: FulltextQuery) -> Search:
    """
    Applies query filters to ES Search object based on query
    """
    # type filters
    if query.filter_type == "papers" or query.filter_type is None:
        search = search.filter(
            "terms", type=["article-journal", "paper-conference", "chapter", "article"]
        )
    elif query.filter_type == "reports":
        search = search.filter("terms", type=["report", "standard"])
    elif query.filter_type == "datasets":
        search = search.filter("terms", type=["dataset", "software"])
    elif query.filter_type == "everything":
        pass
    else:
        raise ValueError(
            f"Unknown 'filter_type' parameter value: '{query.filter_type}'"
        )

    # time filters
    if query.filter_time == "past_week":
        date_today = datetime.date.today()
        week_ago_date = str(date_today - datetime.timedelta(days=7))
        tomorrow_date = str(date_today + datetime.timedelta(days=1))
        search = search.filter("range", date=dict(gte=week_ago_date, lte=tomorrow_date))
    elif query.filter_time == "past_year":
        # (date in the past year) or (year is this year)
        # the later to catch papers which don't have release_date defined
        date_today = datetime.date.today()
        this_year = date_today.year
        tomorrow_date = str(date_today + datetime.timedelta(days=1))
        year_ago_date = str(date_today - datetime.timedelta(days=365))
        search = search.filter(
            Q("range", date=dict(gte=year_ago_date, lte=tomorrow_date))
            | Q("term", year=this_year)
        )
    elif query.filter_time == "since_2000":
        this_year = datetime.date.today().year
        search = search.filter("range", year=dict(gte=2000, lte=this_year))
    elif query.filter_time == "before_1925":
        search = search.filter("range", year=dict(lt=1925))
    elif query.filter_time == "all_time" or query.filter_time is None:
        pass
    else:
        raise ValueError(
            f"Unknown 'filter_time' parameter value: '{query.filter_time}'"
        )

    # availability filters
    if query.filter_availability == "oa":
        search = search.filter("term", tags="oa")
    elif query.filter_availability == "everything":
        pass
    elif query.filter_availability == "fulltext" or query.filter_availability is None:
        search = search.filter(
            "terms", **{"access.access_type": ["wayback", "ia_file", "ia_sim"]}
        )
    elif query.filter_availability == "microfilm":
        search = search.filter("term", **{"access.access_type": "ia_sim"})
    else:
        raise ValueError(
            f"Unknown 'filter_availability' parameter value: '{query.filter_availability}'"
        )

    return search


def process_query(query: FulltextQuery) -> FulltextHits:

    if not query.q:
        return do_fulltext_search(query)

    # try handling raw identifier queries
    if len(query.q.strip().split()) == 1 and '"' not in query.q:
        doi = clean_doi(query.q)
        if doi:
            return do_lookup_query(f'doi:"{doi}"')
        pmcid = clean_pmcid(query.q)
        if pmcid:
            return do_lookup_query(f'pmcid:"{pmcid}"')
        if query.q.strip().startswith("key:"):
            return do_lookup_query(query.q)

    # if this is a citation string, do a fuzzy lookup
    if settings.ENABLE_CITATION_QUERY and sniff_citation_query(query.q):
        api_conf = fatcat_openapi_client.Configuration()
        api_conf.host = settings.FATCAT_API_HOST
        api_client = fatcat_openapi_client.DefaultApi(
            fatcat_openapi_client.ApiClient(api_conf)
        )
        fatcat_es_client = elasticsearch.Elasticsearch("https://search.fatcat.wiki")
        key: Optional[str] = None
        try:
            key = try_fuzzy_match(
                query.q,
                grobid_host=settings.GROBID_HOST,
                es_client=fatcat_es_client,
                fatcat_api_client=api_client,
            )
        except elasticsearch.exceptions.RequestError as e:
            logging.warn(f"citation fuzzy failure: {e}")
            pass
        except Exception as e:
            # TODO: sentry log?
            logging.warn(f"citation fuzzy failure: {e}")
            raise e
        if key:
            result = do_lookup_query(f"key:{key}")
            if result:
                result.query_type = "citation"
                return result

    # fall through to regular query, with pre-parsing
    query = copy.copy(query)
    if query.q:
        query.q = pre_parse_query(query.q)

    return do_fulltext_search(query)


def do_lookup_query(lookup: str) -> FulltextHits:
    logging.info(f"lookup query: {lookup}")
    query = FulltextQuery(
        q=lookup,
        filter_type="everything",
        filter_availability="everything",
        filter_time="all_time",
    )
    result = do_fulltext_search(query)
    result.query_type = "lookup"
    return result


def do_fulltext_search(
    query: FulltextQuery, deep_page_limit: int = 2000
) -> FulltextHits:

    search = Search(using=es_client, index=settings.ELASTICSEARCH_QUERY_FULLTEXT_INDEX)

    if query.collapse_key:
        search = search.filter("term", collapse_key=query.collapse_key)
    else:
        search = search.extra(
            collapse={
                "field": "collapse_key",
                "inner_hits": {"name": "more_pages", "size": 0},
            }
        )

    # apply filters from query
    search = apply_filters(search, query)

    # we combined several queries to improve scoring.

    # this query use the fancy built-in query string parser
    basic_fulltext = Q(
        "query_string",
        query=query.q,
        default_operator="AND",
        analyze_wildcard=True,
        allow_leading_wildcard=False,
        lenient=True,
        quote_field_suffix=".exact",
        fields=["title^4", "biblio_all^3", "everything"],
    )
    has_fulltext = Q("terms", **{"access_type": ["ia_sim", "ia_file", "wayback"]})
    poor_metadata = Q(
        "bool",
        should=[
            # if these fields aren't set, metadata is poor. The more that do
            # not exist, the stronger the signal.
            Q("bool", must_not=Q("exists", field="year")),
            Q("bool", must_not=Q("exists", field="type")),
            Q("bool", must_not=Q("exists", field="stage")),
            Q("bool", must_not=Q("exists", field="biblio.container_name")),
        ],
    )

    if query.filter_availability == "fulltext" or query.filter_availability is None:
        base_query = basic_fulltext
    else:
        base_query = Q("bool", must=basic_fulltext, should=[has_fulltext])

    if query.q == "*":
        search = search.query("match_all")
        search = search.sort("_doc")
    else:
        search = search.query(
            "boosting", positive=base_query, negative=poor_metadata, negative_boost=0.5,
        )

    # simplified version of basic_fulltext query, for highlighting
    highlight_query = Q(
        "query_string", query=query.q, default_operator="AND", lenient=True,
    )
    search = search.highlight(
        "abstracts.body",
        "fulltext.body",
        "fulltext.acknowledgement",
        "fulltext.annex",
        highlight_query=highlight_query.to_dict(),
        require_field_match=False,
        number_of_fragments=2,
        fragment_size=200,
        order="score",
        # TODO: this will fix highlight encoding, but requires ES 7.x
        # encoder="html",
    )

    # sort order
    if query.sort_order == "time_asc":
        search = search.sort("year", "date")
    elif query.sort_order == "time_desc":
        search = search.sort("-year", "-date")
    elif query.sort_order == "relevancy" or query.sort_order is None:
        pass
    else:
        raise ValueError(f"Unknown 'sort_order' parameter value: '{query.sort_order}'")

    # Sanity checks
    limit = min((int(query.limit or 15), 100))
    offset = max((int(query.offset or 0), 0))
    if offset > deep_page_limit:
        # Avoid deep paging problem.
        offset = deep_page_limit

    search = search.params(track_total_hits=True)
    search = search[offset : (offset + limit)]

    query_start = datetime.datetime.now()
    try:
        resp = search.execute()
    except elasticsearch.exceptions.RequestError as e_raw:
        # this is a "user" error
        e: Any = e_raw
        logging.warn("elasticsearch 400: " + str(e.info))
        if e.info.get("error", {}).get("root_cause", {}):
            raise ValueError(str(e.info["error"]["root_cause"][0].get("reason"))) from e
        else:
            raise ValueError(str(e.info)) from e
    except elasticsearch.exceptions.TransportError as e:
        # all other errors
        logging.warn(f"elasticsearch non-200 status code: {e.info}")
        raise IOError(str(e.info)) from e
    query_delta = datetime.datetime.now() - query_start

    # convert from API objects to dicts
    results = transform_es_results(resp)

    count_found: int = 0
    if isinstance(resp.hits.total, int):
        count_found = int(resp.hits.total)
    else:
        count_found = int(resp.hits.total["value"])
    count_returned = len(results)

    # if we grouped to less than a page of hits, update returned count
    if (not query.collapse_key) and offset == 0 and (count_returned < limit):
        count_found = count_returned

    return FulltextHits(
        query_type="fulltext",
        count_returned=count_returned,
        count_found=count_found,
        offset=offset,
        limit=limit,
        deep_page_limit=deep_page_limit,
        query_time_ms=int(resp.took),
        query_wall_time_ms=int(query_delta.total_seconds() * 1000),
        results=results,
    )
