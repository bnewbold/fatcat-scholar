import os
import io
import sys
import argparse
from typing import List, Dict, Tuple, Optional, Any, Sequence
import urllib3.exceptions

import minio
import requests
import sentry_sdk
import internetarchive
from fatcat_openapi_client import ReleaseEntity, FileEntity, WebcaptureEntity

from fatcat_scholar.api_entities import entity_from_json
from fatcat_scholar.config import settings, GIT_REVISION
from fatcat_scholar.djvu import djvu_extract_leaf_texts
from fatcat_scholar.sandcrawler import (
    SandcrawlerPostgrestClient,
    SandcrawlerMinioClient,
)
from fatcat_scholar.issue_db import IssueDB, SimIssueRow, SimPubRow
from fatcat_scholar.schema import (
    DocType,
    IntermediateBundle,
)
from fatcat_scholar.sim_pipeline import truncate_pub_meta, truncate_issue_meta


def parse_pages(raw: str) -> Tuple[Optional[int], Optional[int]]:
    first_raw = raw.split("-")[0]
    if not first_raw.isdigit():
        return (None, None)
    first = int(first_raw)
    if "-" not in raw:
        return (first, first)
    last_raw = raw.split("-")[-1]
    if not last_raw.isdigit():
        return (first, first)
    last = int(last_raw)
    if last < first:
        last_munge = first_raw[0 : (len(first_raw) - len(last_raw))] + last_raw
        last = int(last_munge)
    if last < first:
        return (first, first)
    return (first, last)


def test_parse_pages() -> None:
    assert parse_pages("479-89") == (479, 489)
    assert parse_pages("466-7") == (466, 467)
    assert parse_pages("466-501") == (466, 501)
    assert parse_pages("466-401") == (466, 466)
    # TODO: or should it be (1, 1)?
    assert parse_pages("1") == (1, 1)
    # TODO: should we be doing strings instead of ints?
    assert parse_pages("iiv") == (None, None)


def fulltext_pref_list(releases: List[ReleaseEntity]) -> List[str]:
    """
    Returns a list of release idents in preference order (best first) to
    try and find fulltext for.
    """
    releases_sorted = sorted(
        releases,
        reverse=True,
        key=lambda r: (
            r.release_stage == "updated",
            r.release_stage == "published",
            r.volume is not None,
            r.container_id is not None,
            r.ext_ids.pmid is not None,
            r.release_stage == "submitted",
            r.release_type is not None,
            r.release_year is not None,
            r.release_year,
            r.release_date is not None,
            r.release_date,
            r.version is not None,
            r.version,
        ),
    )
    return [r.ident for r in releases_sorted]


class WorkPipeline:
    def __init__(
        self,
        issue_db: IssueDB,
        sandcrawler_db_client: SandcrawlerPostgrestClient,
        sandcrawler_s3_client: SandcrawlerMinioClient,
    ):
        self.issue_db: IssueDB = issue_db
        self.ia_client = internetarchive.get_session()
        self.sandcrawler_db_client = sandcrawler_db_client
        self.sandcrawler_s3_client = sandcrawler_s3_client

    def fetch_file_grobid(self, fe: FileEntity, release_ident: str) -> Optional[Any]:
        """
        tei_xml: str
        release_ident: Optional[str]
        file_ident: Optional[str]
        """
        if not fe.sha1:
            return None
        if not fe.urls:
            return None
        grobid_meta = self.sandcrawler_db_client.get_grobid(fe.sha1)
        if not grobid_meta or grobid_meta["status"] != "success":
            return None
        # print(grobid_meta)
        try:
            grobid_xml = self.sandcrawler_s3_client.get_blob(
                bucket="sandcrawler",
                prefix="",
                folder="grobid",
                sha1hex=fe.sha1,
                extension=".tei.xml",
            )
            # print(grobid_xml)
        except minio.error.NoSuchKey:
            return None
        except urllib3.exceptions.MaxRetryError:
            # HACK: work around broken seaweedfs keys
            print(f"seaweedfs failure: sha1hex={fe.sha1}", file=sys.stderr)
            return None
        return dict(
            tei_xml=grobid_xml, release_ident=release_ident, file_ident=fe.ident,
        )

    def fetch_pdf_meta(
        self, fe: FileEntity, release_ident: str
    ) -> Optional[Dict[str, Any]]:
        """
        Fetches pdftext metadata from sandcrawler-db via postgrest HTTP
        interface.

        Returns a JSON object on success, or None if not found.

        raw_text: str
        release_ident: Optional[str]
        file_ident: Optional[str]
        """
        if not fe.sha1:
            return None
        pdf_meta = self.sandcrawler_db_client.get_pdf_meta(fe.sha1)
        if not pdf_meta or pdf_meta["status"] != "success":
            return None
        return dict(
            pdf_meta=pdf_meta, release_ident=release_ident, file_ident=fe.ident,
        )

    def fetch_file_pdftotext(self, fe: FileEntity, release_ident: str) -> Optional[Any]:
        """
        raw_text: str
        release_ident: Optional[str]
        file_ident: Optional[str]
        """
        if not fe.sha1:
            return None
        if not fe.urls:
            return None
        try:
            raw_text = self.sandcrawler_s3_client.get_blob(
                bucket="sandcrawler",
                prefix="",
                folder="text",
                sha1hex=fe.sha1,
                extension=".txt",
            )
            # print(raw_text)
        except minio.error.NoSuchKey:
            return None
        except urllib3.exceptions.MaxRetryError:
            # HACK: work around broken seaweedfs keys
            print(f"seaweedfs failure: sha1hex={fe.sha1}", file=sys.stderr)
            return None
        return dict(
            raw_text=raw_text, release_ident=release_ident, file_ident=fe.ident,
        )

    def fetch_webcapture_html_fulltext(
        self, wc: WebcaptureEntity, release_ident: str,
    ) -> Optional[Dict[str, Any]]:

        primary_resources = [cdx for cdx in wc.cdx if cdx.url == wc.original_url]
        if not primary_resources or primary_resources[0].mimetype != "text/html":
            return None
        html_meta = self.sandcrawler_db_client.get_html_meta(primary_resources[0].sha1)
        if not html_meta:
            return None
        sha1hex = html_meta.get("sha1hex")
        if not sha1hex:
            return None
        if html_meta.get("status") != "success" or not html_meta.get("has_teixml"):
            return None

        try:
            tei_xml = self.sandcrawler_s3_client.get_blob(
                bucket="sandcrawler",
                prefix="",
                folder="html_body",
                sha1hex=sha1hex,
                extension=".tei.xml",
            )
            # print(grobid_xml)
        except minio.error.NoSuchKey:
            return None
        except urllib3.exceptions.MaxRetryError:
            # HACK: work around broken seaweedfs keys
            print(f"seaweedfs failure: sha1hex={sha1hex}", file=sys.stderr)
            return None

        return dict(
            html_meta=html_meta,
            tei_xml=tei_xml,
            release_ident=release_ident,
            webcapture_ident=wc.ident,
        )

    def lookup_sim(self, release: ReleaseEntity) -> Optional[SimIssueRow]:
        """
        Checks in IssueDB to see if this release is likely to have a copy in a
        SIM issue item.

        volume
        issue
        """
        if not (release.container_id and release.volume and release.issue):
            return None
        sim_pubid = self.issue_db.container2pubid(release.container_id)
        if not sim_pubid:
            return None

        return self.issue_db.lookup_issue(
            sim_pubid=sim_pubid, volume=release.volume, issue=release.issue
        )

    def fetch_sim(
        self,
        issue_db_row: SimIssueRow,
        issue_db_pub_row: SimPubRow,
        pages: str,
        release_ident: str,
    ) -> Optional[Any]:
        """
        issue_item
        pages: str
        page_texts: list
            page_num
            leaf_num
            raw_text
        release_ident: Optional[str]
        pub_item_metadata
        issue_item_metadata
        """

        first_page, last_page = parse_pages(pages)
        if first_page is None:
            return None

        # fetch full metadata from API
        issue_meta = self.ia_client.get_metadata(issue_db_row.issue_item)
        pub_meta = self.ia_client.get_metadata(issue_db_pub_row.pub_collection)

        leaf_index = dict()
        leaf_list = []
        if "page_numbers" not in issue_meta:
            # TODO: warn
            return None
        for entry in issue_meta["page_numbers"].get("pages", []):
            page_num = entry["pageNumber"]
            leaf_index[entry["leafNum"]] = page_num
            if not (page_num and page_num.isdigit()):
                continue
            page_num = int(page_num)
            if page_num >= first_page and page_num <= last_page:
                leaf_list.append(entry["leafNum"])

        if not leaf_list:
            return None

        page_texts: List[Dict[str, Any]] = []
        issue_item = self.ia_client.get_item(issue_db_row.issue_item)
        issue_item_djvu = issue_item.get_file(issue_db_row.issue_item + "_djvu.xml")

        # override 'close()' method so we can still read out contents
        djvu_bytes = io.BytesIO()
        djvu_bytes.close = lambda: None  # type: ignore
        assert issue_item_djvu.download(fileobj=djvu_bytes)
        djvu_bytes.seek(0)
        djvu_xml = io.StringIO(djvu_bytes.read().decode("UTF-8"))
        del djvu_bytes

        leaf_dict = djvu_extract_leaf_texts(djvu_xml, only_leaves=leaf_list)

        for leaf_num, raw_text in leaf_dict.items():
            page_texts.append(
                dict(
                    page_num=leaf_index.get(leaf_num),
                    leaf_num=leaf_num,
                    raw_text=raw_text,
                )
            )

        return dict(
            issue_item=issue_db_row.issue_item,
            pages=pages,
            page_texts=page_texts,
            release_ident=release_ident,
            pub_item_metadata=truncate_pub_meta(pub_meta),
            issue_item_metadata=truncate_issue_meta(issue_meta),
        )

    def process_release_list(self, releases: List[ReleaseEntity]) -> IntermediateBundle:
        """
        1. find best accessible fatcat file
            => fetch GROBID XML if available, else pdftotext if available
            => link to thumbnail if available
        2. find best SIM microfilm copy available
        """
        pref_idents = fulltext_pref_list(releases)
        release_dict = {r.ident: r for r in releases}

        # print(f"pref_idents={pref_idents}", file=sys.stderr)

        # find best accessible fatcat file
        grobid_fulltext: Optional[Any] = None
        pdf_meta: Optional[Any] = None
        pdftotext_fulltext: Optional[Any] = None
        html_fulltext: Optional[Any] = None
        for ident in pref_idents:
            release = release_dict[ident]
            if not (release.files or release.webcaptures):
                continue
            for fe in release.files:
                if not fe.sha1 or fe.mimetype not in (None, "application/pdf"):
                    continue
                if not fe.urls:
                    continue
                grobid_fulltext = self.fetch_file_grobid(fe, ident)
                pdf_meta = self.fetch_pdf_meta(fe, ident)
                pdftotext_fulltext = None
                if pdf_meta and not grobid_fulltext:
                    pdftotext_fulltext = self.fetch_file_pdftotext(fe, ident)
                if grobid_fulltext or pdftotext_fulltext:
                    break
                pdf_meta = None
            for wc in release.webcaptures:
                # find primary web capture object
                html_fulltext = self.fetch_webcapture_html_fulltext(wc, ident)
                if html_fulltext and html_fulltext.get("tei_xml"):
                    break
                html_fulltext = None
            if grobid_fulltext or pdftotext_fulltext or html_fulltext:
                break

        # find best accessible SIM metadata and fulltext
        sim_fulltext: Optional[Any] = None
        sim_issue: Optional[Any] = None
        for ident in pref_idents:
            release = release_dict[ident]
            # print(f"{release.extra}\n{release.pages}", file=sys.stderr)
            if not release.pages:
                continue
            # TODO: in the future, will use release.extra.ia.sim.sim_pubid for lookup
            sim_issue = self.lookup_sim(release)
            # print(f"release_{release.ident}: sim_issue={sim_issue}", file=sys.stderr)
            if not sim_issue:
                continue
            sim_pub = self.issue_db.lookup_pub(sim_issue.sim_pubid)
            if not sim_pub:
                continue
            # XXX: control flow tweak?
            try:
                sim_fulltext = self.fetch_sim(
                    sim_issue, sim_pub, release.pages, release.ident
                )
            except requests.exceptions.ConnectionError as e:
                print(str(e), file=sys.stderr)
                continue
            except requests.exceptions.ReadTimeout as e:
                print(str(e), file=sys.stderr)
                continue
            if sim_fulltext:
                break

        return IntermediateBundle(
            doc_type=DocType.work,
            releases=releases,
            biblio_release_ident=pref_idents[0],
            grobid_fulltext=grobid_fulltext,
            pdftotext_fulltext=pdftotext_fulltext,
            pdf_meta=pdf_meta,
            html_fulltext=html_fulltext,
            sim_fulltext=sim_fulltext,
        )

    def run_releases(self, release_stream: Sequence[str]) -> None:
        """
        Iterates over the stream of releases, which are expected to be grouped
        (sorted) by work_ident.

        Collects releases under same work_ident into a batch and processes a
        work from that.

        TODO: what is the right API here? stream iterator? how should
        parallelism work?
        """
        batch = []
        batch_work_id = None
        for line in release_stream:
            if not line:
                continue
            release = entity_from_json(line, ReleaseEntity)
            if release.work_id == batch_work_id:
                batch.append(release)
                continue
            if batch:
                ib = self.process_release_list(batch)
                print(ib.json(exclude_none=True, sort_keys=True))
                batch_work_id = None
            batch = [
                release,
            ]
            batch_work_id = release.work_id

        if batch:
            ib = self.process_release_list(batch)
            print(ib.json(exclude_none=True, sort_keys=True))


def main() -> None:
    """
    Run this command like:

        python -m fatcat_scholar.work_pipeline
    """

    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    subparsers = parser.add_subparsers()

    parser.add_argument(
        "--issue-db-file",
        help="sqlite3 database file to open",
        default="data/issue_db.sqlite",
        type=str,
    )
    parser.add_argument(
        "--sandcrawler-db-api",
        help="Sandcrawler Postgrest API endpoint",
        default=settings.SANDCRAWLER_DB_API,
        type=str,
    )
    parser.add_argument(
        "--sandcrawler-s3-api",
        help="Sandcrawler S3 (minio/seaweedfs) API endpoint",
        default=settings.SANDCRAWLER_S3_API,
        type=str,
    )

    sub = subparsers.add_parser(
        "run_releases", help="takes expanded release entity JSON, sorted by work_ident"
    )
    sub.set_defaults(func="run_releases")
    sub.add_argument(
        "json_file",
        help="release entities, as JSON-lines",
        nargs="?",
        default=sys.stdin,
        type=argparse.FileType("r"),
    )

    args = parser.parse_args()
    if not args.__dict__.get("func"):
        parser.print_help(file=sys.stderr)
        sys.exit(-1)

    if settings.SENTRY_DSN:
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment=settings.SCHOLAR_ENV,
            max_breadcrumbs=10,
            release=GIT_REVISION,
        )

    wp = WorkPipeline(
        issue_db=IssueDB(args.issue_db_file),
        sandcrawler_db_client=SandcrawlerPostgrestClient(
            api_url=args.sandcrawler_db_api
        ),
        sandcrawler_s3_client=SandcrawlerMinioClient(
            host_url=args.sandcrawler_s3_api,
            access_key=os.environ.get("MINIO_ACCESS_KEY"),
            secret_key=os.environ.get("MINIO_SECRET_KEY"),
        ),
    )

    if args.func == "run_releases":
        wp.run_releases(args.json_file)
    else:
        func = getattr(wp, args.func)
        func()


if __name__ == "__main__":
    main()
