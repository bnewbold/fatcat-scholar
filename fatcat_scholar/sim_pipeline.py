import io
import sys
import sqlite3
import argparse
from typing import List, Dict, Optional, Any
import urllib3.exceptions

import requests
import sentry_sdk
import internetarchive

from fatcat_scholar.config import settings, GIT_REVISION
from fatcat_scholar.djvu import djvu_extract_leaf_texts
from fatcat_scholar.issue_db import IssueDB
from fatcat_scholar.schema import (
    DocType,
    IntermediateBundle,
)


def truncate_pub_meta(full: Dict[str, Any]) -> Dict[str, Any]:
    """
    Takes a complete archive.org metadata dictionary for a publication
    collection, and simplifies it by removing fields. Motivation is to make
    intermediate bundle files smaller.
    """
    full.pop("files")
    if "ulrichs" in full and full["ulrichs"]:
        full["ulrichs"][0].pop("reviews_mfl")
        full["ulrichs"][0].pop("editorial_description")

        # these are interesting, but just too long
        full["ulrichs"][0].pop("online_availability_full_text")
        full["ulrichs"][0].pop("abstracting_indexing")
        full["ulrichs"][0].pop("publisher_and_ordering_details")
    return full


def truncate_issue_meta(full: Dict[str, Any]) -> Dict[str, Any]:
    """
    Same as truncate_pub_meta() but for issue item metadata
    """
    full.pop("files")
    full.pop("histograms", None)
    full.pop("rotations", None)
    return full


class SimPipeline:
    def __init__(self, issue_db: IssueDB):
        self.issue_db: IssueDB = issue_db
        self.ia_client = internetarchive.get_session()

    def fetch_sim_issue(self, issue_item: str, pub_collection: str) -> Optional[Any]:
        """
        issue_item
        pages: str
        page_texts: list
            raw_text
            page_num
            leaf_num
        release_ident: Optional[str]
        pub_item_metadata
        issue_item_metadata
        """
        # fetch full metadata from API
        issue_meta = self.ia_client.get_metadata(issue_item)
        pub_meta = self.ia_client.get_metadata(pub_collection)

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
            leaf_list.append(entry["leafNum"])

        if not leaf_list:
            return None

        page_texts: List[Dict[str, Any]] = []
        issue_item_obj = self.ia_client.get_item(issue_item)
        issue_item_djvu = issue_item_obj.get_file(issue_item + "_djvu.xml")

        # override 'close()' method so we can still read out contents
        djvu_bytes = io.BytesIO()
        djvu_bytes.close = lambda: None  # type: ignore
        assert issue_item_djvu.download(fileobj=djvu_bytes)
        djvu_bytes.seek(0)
        djvu_xml = io.StringIO(djvu_bytes.read().decode("UTF-8"))
        del djvu_bytes

        leaf_dict = djvu_extract_leaf_texts(djvu_xml)

        for leaf_num, raw_text in leaf_dict.items():
            page_texts.append(
                dict(
                    page_num=leaf_index.get(leaf_num),
                    leaf_num=leaf_num,
                    raw_text=raw_text,
                )
            )

        return dict(
            issue_item=issue_item,
            pages=None,
            page_texts=page_texts,
            release_ident=None,
            pub_item_metadata=truncate_pub_meta(pub_meta),
            issue_item_metadata=truncate_issue_meta(issue_meta),
        )

    def full_issue_to_pages(self, full_issue: dict) -> List[IntermediateBundle]:
        pages = []
        for leaf in full_issue["page_texts"]:
            bundle = IntermediateBundle(
                doc_type=DocType.sim_page,
                releases=[],
                biblio_release_ident=None,
                grobid_fulltext=None,
                pdftotext_fulltext=None,
                sim_fulltext=dict(
                    issue_item=full_issue["issue_item"],
                    pages=str(leaf["page_num"]),
                    page_texts=[leaf],
                    release_ident=None,
                    pub_item_metadata=full_issue["pub_item_metadata"],
                    issue_item_metadata=full_issue["issue_item_metadata"],
                ),
            )
            pages.append(bundle)
        return pages

    def run_issue_db(self, limit: int = None) -> None:
        """
        Legacy/Deprecated code path

        Runs DB iteration and fetching in single thread/process
        """
        count = 0
        self.issue_db.db.row_factory = sqlite3.Row
        cur = self.issue_db.db.cursor()
        for row in cur.execute(
            "SELECT * FROM sim_issue LEFT JOIN sim_pub ON sim_issue.sim_pubid = sim_pub.sim_pubid WHERE sim_issue.release_count < 3"
        ):
            # filter out "contents" and "index" items
            # TODO: more filters; also redundant with IssueDB code?
            if row["issue_item"].endswith("_contents") or row["issue_item"].endswith(
                "_index"
            ):
                continue
            try:
                full_issue = self.fetch_sim_issue(
                    row["issue_item"], row["pub_collection"]
                )
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.RetryError,
                urllib3.exceptions.MaxRetryError,
            ) as e:
                print(str(e), file=sys.stderr)
                continue
            if not full_issue:
                continue
            pages = self.full_issue_to_pages(full_issue)
            for bundle in pages:
                print(bundle.json(exclude_none=True, sort_keys=True))
                count += 1
                if limit is not None and count >= limit:
                    break
            if limit is not None and count >= limit:
                break

    def run_print_issues(self, max_release_count: int = 5) -> None:
        """
        Iterates over issues, printing as TSV to stdout

        Intended to be used with GNU/parallel for coarse parallelism.
        """
        self.issue_db.db.row_factory = sqlite3.Row
        cur = self.issue_db.db.cursor()
        for row in cur.execute(
            f"SELECT * FROM sim_issue LEFT JOIN sim_pub ON sim_issue.sim_pubid = sim_pub.sim_pubid WHERE sim_issue.release_count < {max_release_count}"
        ):
            # filter out "contents" and "index" items
            # TODO: more filters; also redundant with IssueDB code?
            if row["issue_item"].endswith("_contents") or row["issue_item"].endswith(
                "_index"
            ):
                continue
            print(f"{row['issue_item']}\t{row['pub_collection']}")

    def run_fetch_issue(self, issue_item: str, pub_collection: str) -> None:
        """
        Fetches SIM issue.

        TODO: more advanced retries?
        """
        try:
            full_issue = self.fetch_sim_issue(issue_item, pub_collection)
        except requests.exceptions.ConnectionError as e:
            print(str(e), file=sys.stderr)
            return
        except requests.exceptions.ReadTimeout as e:
            print(str(e), file=sys.stderr)
            return
        if not full_issue:
            return
        pages = self.full_issue_to_pages(full_issue)
        for bundle in pages:
            print(bundle.json(exclude_none=True, sort_keys=True))


def main() -> None:
    """
    Run this command like:

        python -m fatcat_scholar.sim_pipeline
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

    sub = subparsers.add_parser("run_issue_db", help="iterates through entire IssueDB")
    sub.set_defaults(func="run_issue_db")
    sub.add_argument("--limit", help="maximum number of pages to index", type=int)

    sub = subparsers.add_parser(
        "run_print_issues",
        help="dumps issues as simple TSV rows (for parallel processing)",
    )
    sub.set_defaults(func="run_print_issues")

    sub = subparsers.add_parser(
        "run_fetch_issue", help="fetches pages for given issue item"
    )
    sub.add_argument("issue_item", type=str)
    sub.add_argument("pub_collection", type=str)
    sub.set_defaults(func="run_fetch_issue")

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

    sp = SimPipeline(issue_db=IssueDB(args.issue_db_file))

    if args.func == "run_issue_db":
        sp.run_issue_db(limit=args.limit)
    elif args.func == "run_print_issues":
        sp.run_print_issues()
    elif args.func == "run_fetch_issue":
        sp.run_fetch_issue(
            issue_item=args.issue_item, pub_collection=args.pub_collection
        )
    else:
        func = getattr(sp, args.func)
        func()


if __name__ == "__main__":
    main()
