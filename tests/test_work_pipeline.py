from typing import Any

import responses

from fatcat_scholar.issue_db import IssueDB
from fatcat_scholar.sandcrawler import (
    SandcrawlerPostgrestClient,
    SandcrawlerMinioClient,
)
from fatcat_scholar.work_pipeline import WorkPipeline
from fatcat_scholar.config import settings


@responses.activate
def test_run_transform(mocker: Any) -> None:

    issue_db = IssueDB(settings.SCHOLAR_ISSUEDB_PATH)
    issue_db.init_db()

    responses.add(
        responses.GET,
        "http://disabled-during-tests-bogus.xyz:3333/grobid?sha1hex=eq.bca1531b0562c6d72e0c283c1ccb97eb5cb02117",
        status=200,
        json=[
            {
                "sha1hex": "bca1531b0562c6d72e0c283c1ccb97eb5cb02117",
                "updated": "2019-11-30T04:44:00+00:00",
                "grobid_version": "0.5.5-fatcat",
                "status_code": 200,
                "status": "success",
                "fatcat_release": "hsmo6p4smrganpb3fndaj2lon4",
                "metadata": {
                    "biblio": {
                        "doi": "10.7717/peerj.4375",
                        "date": "2018-02-13",
                        "title": "Distributed under Creative Commons CC-BY 4.0 The state of OA: a large-scale analysis of the prevalence and impact of Open Access articles",
                        "authors": [],
                    },
                    "language_code": "en",
                    "grobid_timestamp": "2019-11-30T04:44+0000",
                },
            }
        ],
    )

    responses.add(
        responses.GET,
        "http://disabled-during-tests-bogus.xyz:3333/pdf_meta?sha1hex=eq.bca1531b0562c6d72e0c283c1ccb97eb5cb02117",
        status=200,
        json=[
            {
                "sha1hex": "bca1531b0562c6d72e0c283c1ccb97eb5cb02117",
                "updated": "2020-07-07T02:15:52.98309+00:00",
                "status": "success",
                "has_page0_thumbnail": True,
                "page_count": 23,
                "word_count": 10534,
                "page0_height": 792,
                "page0_width": 612,
                "permanent_id": "52f2164b9cc9e47fd150e7ee389b595a",
                "pdf_created": "2018-02-09T06:06:06+00:00",
                "pdf_version": "1.5",
                "metadata": {
                    "title": "",
                    "author": "",
                    "creator": "River Valley",
                    "subject": "Legal Issues, Science Policy, Data Science",
                    "producer": "pdfTeX-1.40.16",
                },
            }
        ],
    )

    es_raw = mocker.patch("fatcat_scholar.work_pipeline.WorkPipeline.fetch_file_grobid")
    es_raw.side_effect = [
        {"tei_xml": "<xml>dummy", "release_ident": "asdf123", "file_ident": "xyq9876"},
    ]

    wp = WorkPipeline(
        issue_db=issue_db,
        sandcrawler_db_client=SandcrawlerPostgrestClient(
            api_url=settings.SANDCRAWLER_DB_API
        ),
        sandcrawler_s3_client=SandcrawlerMinioClient(
            host_url=settings.SANDCRAWLER_S3_API
        ),
    )

    with open("tests/files/release_hsmo6p4smrganpb3fndaj2lon4_sans.json", "r") as f:
        wp.run_releases(f.readlines())
