import sys
import json
import sqlite3
import argparse
from dataclasses import dataclass
from typing import List, Dict, Optional, Any, Sequence, Tuple
import fatcat_openapi_client
import elasticsearch
from elasticsearch_dsl import Search

from fatcat_scholar.config import settings


@dataclass
class SimPubRow:
    sim_pubid: str
    pub_collection: str
    title: str
    issn: Optional[str]
    pub_type: Optional[str]
    publisher: Optional[str]

    container_issnl: Optional[str]
    container_ident: Optional[str]
    wikidata_qid: Optional[str]

    def tuple(self) -> Tuple:
        return (
            self.sim_pubid,
            self.pub_collection,
            self.title,
            self.issn,
            self.pub_type,
            self.publisher,
            self.container_issnl,
            self.container_ident,
            self.wikidata_qid,
        )

    @classmethod
    def from_tuple(cls, row: Any) -> "SimPubRow":
        return SimPubRow(
            sim_pubid=row[0],
            pub_collection=row[1],
            title=row[2],
            issn=row[3],
            pub_type=row[4],
            publisher=row[5],
            container_issnl=row[6],
            container_ident=row[7],
            wikidata_qid=row[8],
        )


@dataclass
class SimIssueRow:
    """
    TODO:
    - distinguish between release count that can do full link with pages, or
      just in this year/volume/issue?
    """

    issue_item: str
    sim_pubid: str
    year: Optional[int]
    volume: Optional[str]
    issue: Optional[str]
    first_page: Optional[int]
    last_page: Optional[int]
    release_count: Optional[int]

    def tuple(self) -> Tuple:
        return (
            self.issue_item,
            self.sim_pubid,
            self.year,
            self.volume,
            self.issue,
            self.first_page,
            self.last_page,
            self.release_count,
        )

    @classmethod
    def from_tuple(cls, row: Any) -> "SimIssueRow":
        return SimIssueRow(
            issue_item=row[0],
            sim_pubid=row[1],
            year=row[2],
            volume=row[3],
            issue=row[4],
            first_page=row[5],
            last_page=row[6],
            release_count=row[7],
        )


@dataclass
class ReleaseCountsRow:
    sim_pubid: str
    year_in_sim: bool
    release_count: int
    year: Optional[int]
    volume: Optional[str]

    def tuple(self) -> Tuple:
        return (
            self.sim_pubid,
            self.year,
            self.volume,
            self.year_in_sim,
            self.release_count,
        )


def es_issue_count(
    es_client: Any, container_id: str, year: int, volume: str, issue: str
) -> int:
    search = Search(using=es_client, index="fatcat_release")
    search = (
        search.filter("term", container_id=container_id)
        .filter("term", year=year)
        .filter("term", volume=volume)
        .filter("term", issue=issue)
        .extra(request_cache=True)
    )
    search = search.params()

    return search.count()


def es_container_aggs(es_client: Any, container_id: str) -> List[Dict[str, Any]]:
    """
    What is being returned is a list of dicts, each with year, volume, count
    keys.
    """
    search = Search(using=es_client, index="fatcat_release")
    search = search.filter("term", container_id=container_id)
    search.aggs.bucket("years", "terms", field="year").bucket(
        "volumes", "terms", field="volume"
    )
    search = search[:0]
    res = search.execute()
    ret = []
    for year in res.aggregations.years.buckets:
        for volume in year.volumes.buckets:
            ret.append(dict(count=volume.doc_count, year=year.key, volume=volume.key))
            # print(ret[-1])
    return ret


class IssueDB:
    def __init__(self, db_file: str):
        """
        To create a temporary database, pass ":memory:" as db_file
        """
        self.db = sqlite3.connect(db_file, isolation_level="EXCLUSIVE")
        self._pubid2container_map: Dict[str, Optional[str]] = dict()
        self._container2pubid_map: Dict[str, Optional[str]] = dict()

    def init_db(self) -> None:
        self.db.executescript(
            """
            PRAGMA main.page_size = 4096;
            PRAGMA main.cache_size = 20000;
            PRAGMA main.locking_mode = EXCLUSIVE;
            PRAGMA main.synchronous = OFF;
        """
        )
        with open("schema/issue_db.sql", "r") as fschema:
            self.db.executescript(fschema.read())

    def insert_sim_pub(self, pub: SimPubRow, cur: Any = None) -> None:
        if not cur:
            cur = self.db.cursor()
        # print(pub.tuple(), file=sys.stderr)
        cur.execute(
            "INSERT OR REPLACE INTO sim_pub VALUES (?,?,?,?,?,?,?,?,?)", pub.tuple()
        )

    def insert_sim_issue(self, issue: SimIssueRow, cur: Any = None) -> None:
        if not cur:
            cur = self.db.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO sim_issue VALUES (?,?,?,?,?,?,?,?)", issue.tuple()
        )

    def insert_release_counts(self, counts: ReleaseCountsRow, cur: Any = None) -> None:
        if not cur:
            cur = self.db.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO release_counts VALUES (?,?,?,?,?)", counts.tuple()
        )

    def pubid2container(self, sim_pubid: str) -> Optional[str]:
        if sim_pubid in self._pubid2container_map:
            return self._pubid2container_map[sim_pubid]
        row = list(
            self.db.execute(
                "SELECT container_ident FROM sim_pub WHERE sim_pubid = ?;", [sim_pubid]
            )
        )
        if row:
            self._pubid2container_map[sim_pubid] = row[0][0]
            return row[0][0]
        else:
            self._pubid2container_map[sim_pubid] = None
            return None

    def container2pubid(self, container_ident: str) -> Optional[str]:
        if container_ident in self._container2pubid_map:
            return self._container2pubid_map[container_ident]
        row = list(
            self.db.execute(
                "SELECT sim_pubid FROM sim_pub WHERE container_ident = ?;",
                [container_ident],
            )
        )
        if row:
            self._container2pubid_map[container_ident] = row[0][0]
            return row[0][0]
        else:
            self._pubid2container_map[container_ident] = None
            return None

    def lookup_issue(
        self, sim_pubid: str, volume: str, issue: str
    ) -> Optional[SimIssueRow]:
        row = list(
            self.db.execute(
                "SELECT * FROM sim_issue WHERE sim_pubid = ? AND volume = ? AND issue = ?;",
                [sim_pubid, volume, issue],
            )
        )
        if not row:
            return None
        return SimIssueRow.from_tuple(row[0])

    def lookup_pub(self, sim_pubid: str) -> Optional[SimPubRow]:
        row = list(
            self.db.execute("SELECT * FROM sim_pub WHERE sim_pubid = ?;", [sim_pubid])
        )
        if not row:
            return None
        return SimPubRow.from_tuple(row[0])

    def load_pubs(self, json_lines: Sequence[str], api: Any) -> None:
        """
        Reads a file (or some other iterator) of JSON lines, parses them into a
        dict, then inserts rows.
        """
        cur = self.db.cursor()
        for line in json_lines:
            if not line:
                continue
            obj = json.loads(line)
            if "metadata" not in obj:
                continue
            meta = obj["metadata"]
            assert "periodicals" in meta["collection"]
            container: Optional[fatcat_openapi_client.ContainerEntity] = None
            if meta.get("issn") and len(meta["issn"]) == 9:
                try:
                    container = api.lookup_container(issnl=meta["issn"])
                except fatcat_openapi_client.ApiException as ae:
                    if ae.status != 404:
                        raise ae
            row = SimPubRow(
                sim_pubid=meta["sim_pubid"],
                pub_collection=meta["identifier"],
                title=meta["title"],
                issn=meta.get("issn"),
                pub_type=meta.get("pub_type"),
                publisher=meta.get("publisher"),
                container_issnl=container and container.issnl,
                container_ident=container and container.ident,
                wikidata_qid=container and container.wikidata_qid,
            )
            if isinstance(row.publisher, list):
                row.publisher = row.publisher[0]
            self.insert_sim_pub(row, cur)
        cur.close()
        self.db.commit()

    def load_issues(self, json_lines: Sequence[str], es_client: Any) -> None:
        """
        Reads a file (or some other iterator) of JSON lines, parses them into a
        dict, then inserts rows.
        """
        cur = self.db.cursor()
        for line in json_lines:
            if not line:
                continue
            obj = json.loads(line)
            if "metadata" not in obj:
                continue
            meta = obj["metadata"]
            assert "periodicals" in meta["collection"]
            # pub_collection = [c for c in meta['collection'] if c.startswith("pub_")][0]
            issue_item = meta["identifier"]

            # don't index meta items
            # TODO: handle more weird suffixes like "1-2", "_part_1", "_index-contents"
            if issue_item.endswith("_index") or issue_item.endswith("_contents"):
                continue

            sim_pubid = meta["sim_pubid"]

            year: Optional[int] = None
            if meta.get("date") and meta["date"][:4].isdigit():
                year = int(meta["date"][:4])
            volume = meta.get("volume")
            issue = meta.get("issue")

            first_page: Optional[int] = None
            last_page: Optional[int] = None
            if obj.get("page_numbers"):
                pages = [
                    p["pageNumber"]
                    for p in obj["page_numbers"]["pages"]
                    if p["pageNumber"]
                ]
                pages = [int(p) for p in pages if p.isdigit()]
                if len(pages):
                    first_page = min(pages)
                    last_page = max(pages)

            release_count: Optional[int] = None
            if year and volume and issue:
                container_id = self.pubid2container(sim_pubid)
                if container_id:
                    release_count = es_issue_count(
                        es_client, container_id, year, volume, issue
                    )

            row = SimIssueRow(
                issue_item=issue_item,
                sim_pubid=sim_pubid,
                year=year,
                volume=volume,
                issue=issue,
                first_page=first_page,
                last_page=last_page,
                release_count=release_count,
            )
            self.insert_sim_issue(row, cur)
        cur.close()
        self.db.commit()

    def load_counts(self, es_client: Any) -> None:
        all_pub_containers = list(
            self.db.execute(
                "SELECT sim_pubid, container_ident FROM sim_pub WHERE container_ident IS NOT NULL;"
            )
        )
        print(
            f"Loading fatcat container counts for {len(all_pub_containers)} entities...",
            file=sys.stderr,
        )
        cur: Any = self.db.cursor()
        count = 0
        for (sim_pubid, container_ident) in all_pub_containers:
            count += 1
            if count % 500 == 0:
                print(f"  {count}...", file=sys.stderr)
            aggs = es_container_aggs(es_client, container_ident)
            for agg in aggs:
                row = ReleaseCountsRow(
                    sim_pubid=sim_pubid,
                    year_in_sim=False,  # TODO
                    release_count=agg["count"],
                    year=agg["year"],
                    volume=agg["volume"],
                )
                self.insert_release_counts(row, cur)
        cur.close()
        self.db.commit()


def main() -> None:
    """
    Run this command like:

        python -m fatcat_scholar.issue_db
    """

    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    subparsers = parser.add_subparsers()

    parser.add_argument(
        "--db-file",
        help="sqlite3 database file to open",
        default=settings.SCHOLAR_ISSUEDB_PATH,
        type=str,
    )

    sub = subparsers.add_parser("init_db", help="create sqlite3 output file and tables")
    sub.set_defaults(func="init_db")

    sub = subparsers.add_parser(
        "load_pubs", help="update container-level stats from JSON file"
    )
    sub.set_defaults(func="load_pubs")
    sub.add_argument(
        "json_file",
        help="collection-level metadata, as JSON-lines",
        nargs="?",
        default=sys.stdin,
        type=argparse.FileType("r"),
    )

    sub = subparsers.add_parser(
        "load_issues", help="update item-level stats from JSON file"
    )
    sub.set_defaults(func="load_issues")
    sub.add_argument(
        "json_file",
        help="item-level metadata, as JSON-lines",
        nargs="?",
        default=sys.stdin,
        type=argparse.FileType("r"),
    )

    sub = subparsers.add_parser(
        "load_counts", help="update volume-level stats from elasticsearch endpoint"
    )
    sub.set_defaults(func="load_counts")

    args = parser.parse_args()
    if not args.__dict__.get("func"):
        parser.print_help(file=sys.stderr)
        sys.exit(-1)

    idb = IssueDB(args.db_file)
    api_conf = fatcat_openapi_client.Configuration()
    api_conf.host = settings.FATCAT_API_HOST
    api = fatcat_openapi_client.DefaultApi(fatcat_openapi_client.ApiClient(api_conf))
    es_client = elasticsearch.Elasticsearch(settings.ELASTICSEARCH_FATCAT_BASE)

    if args.func == "load_pubs":
        idb.load_pubs(args.json_file, api)
    elif args.func == "load_issues":
        idb.load_issues(args.json_file, es_client)
    elif args.func == "load_counts":
        idb.load_counts(es_client)
    else:
        func = getattr(idb, args.func)
        func()


if __name__ == "__main__":
    main()
