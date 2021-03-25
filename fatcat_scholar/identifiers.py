import re
from typing import Optional

DOI_REGEX = re.compile(r"^10.\d{3,6}/\S+$")


def clean_doi(raw: Optional[str]) -> Optional[str]:
    """
    Removes any:
    - padding whitespace
    - 'doi:' prefix
    - URL prefix

    Does not try to un-URL-encode

    Returns None if not a valid DOI
    """
    if not raw:
        return None
    raw = raw.strip().lower()
    if "\u2013" in raw:
        # Do not attempt to normalize "en dash" and since FC does not allow
        # unicode in DOI, treat this as invalid.
        return None
    if len(raw.split()) != 1:
        return None
    if raw.startswith("doi:"):
        raw = raw[4:]
    if raw.startswith("http://"):
        raw = raw[7:]
    if raw.startswith("https://"):
        raw = raw[8:]
    if raw.startswith("doi.org/"):
        raw = raw[8:]
    if raw.startswith("dx.doi.org/"):
        raw = raw[11:]
    if raw[7:9] == "//":
        raw = raw[:8] + raw[9:]

    # fatcatd uses same REGEX, but Rust regex rejects these characters, while
    # python doesn't. DOIs are syntaxtually valid, but very likely to be typos;
    # for now filter them out.
    for c in ("¬",):
        if c in raw:
            return None

    if not raw.startswith("10."):
        return None
    if not DOI_REGEX.fullmatch(raw):
        return None
    # will likely want to expand DOI_REGEX to exclude non-ASCII characters, but
    # for now block specific characters so we can get PubMed importer running
    # again.
    if "ä" in raw:
        return None
    return raw


def test_clean_doi() -> None:
    assert clean_doi(None) is None
    assert clean_doi("") is None
    assert clean_doi("asdf") is None
    assert clean_doi("10.123") is None
    assert clean_doi("10.1234/asdf ") == "10.1234/asdf"
    assert clean_doi("10.1234/ASdf ") == "10.1234/asdf"
    assert clean_doi("10.1037//0002-9432.72.1.50") == "10.1037/0002-9432.72.1.50"
    assert clean_doi("10.1037/0002-9432.72.1.50") == "10.1037/0002-9432.72.1.50"
    assert clean_doi("10.23750/abm.v88i2 -s.6506") is None
    assert clean_doi("10.17167/mksz.2017.2.129–155") is None
    assert clean_doi("http://doi.org/10.1234/asdf ") == "10.1234/asdf"
    assert clean_doi("https://dx.doi.org/10.1234/asdf ") == "10.1234/asdf"
    assert clean_doi("doi:10.1234/asdf ") == "10.1234/asdf"
    assert clean_doi("doi:10.1234/ asdf ") is None
    assert clean_doi("10.4149/gpb¬_2017042") is None  # "logical negation" character
    assert (
        clean_doi("10.6002/ect.2020.häyry") is None
    )  # this example via pubmed (pmid:32519616)


def clean_pmcid(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    raw = raw.strip().upper()
    if len(raw.split()) != 1:
        return None
    if raw.startswith("PMC") and raw[3:] and raw[3:].isdigit():
        return raw
    return None


def test_clean_pmcid() -> None:
    assert clean_pmcid("10.1234/asdf ") is None
    assert clean_pmcid("") is None
    assert clean_pmcid("1 2") is None
    assert clean_pmcid(None) is None
    assert clean_pmcid("PMC123") == "PMC123"
    assert clean_pmcid("pmc123") == "PMC123"
