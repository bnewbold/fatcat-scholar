import json

from fatcat_openapi_client import ReleaseEntity

from fatcat_scholar.schema import es_biblio_from_release, es_release_from_release, ScholarBiblio
from fatcat_scholar.api_entities import entity_from_json
from fatcat_scholar.transform import biblio_metadata_hacks, run_refs, run_transform


def test_es_release_from_release() -> None:

    with open("tests/files/release_hsmo6p4smrganpb3fndaj2lon4.json", "r") as f:
        release = entity_from_json(f.read(), ReleaseEntity)

    obj = es_release_from_release(release)
    d = json.loads(obj.json())

    assert obj.ident == release.ident == d["ident"] == "hsmo6p4smrganpb3fndaj2lon4"
    assert obj.doi_registrar == "crossref"
    assert obj.doi_prefix == "10.7717"


def test_es_biblio_from_release() -> None:

    with open("tests/files/release_hsmo6p4smrganpb3fndaj2lon4.json", "r") as f:
        release = entity_from_json(f.read(), ReleaseEntity)

    obj = es_biblio_from_release(release)
    d = json.loads(obj.json())

    assert (
        obj.release_ident
        == release.ident
        == d["release_ident"]
        == "hsmo6p4smrganpb3fndaj2lon4"
    )


def test_run_refs() -> None:

    with open("tests/files/work_iarm6swodra2bcrzhxrfaah7py_bundle.json", "r") as f:
        run_refs(f.readlines())


def test_run_transform() -> None:

    with open("tests/files/work_iarm6swodra2bcrzhxrfaah7py_bundle.json", "r") as f:
        run_transform(f.readlines())

    with open("tests/files/sim_page_bundle.json", "r") as f:
        run_transform(f.readlines())


def test_biblio_metadata_hacks() -> None:
    biblio = ScholarBiblio(
        title="some example paper",
        contrib_names=["able seaperson"],
        release_year=2000,
        volume="10",
        issue="5",
        publisher="some university",
        issns=[],
        affiliations=[],
    )
    out = biblio_metadata_hacks(biblio)

    biblio.release_year = 2030
    out = biblio_metadata_hacks(biblio)
    assert out.release_year is None

    biblio.doi_prefix = "10.1101"
    biblio.container_name = None
    biblio.release_stage = None
    biblio.release_type = "post"
    out = biblio_metadata_hacks(biblio)
    assert out.container_name
    assert out.release_stage == "submitted"
    assert out.release_type == "article"
