"""
This file contains helpers for pre-parsing and transforming search query
strings. See the "basic query parsing" proposal doc for original motivation and
design details.
"""

import re
import shlex


def _clean_token(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return f'"{raw}"'
    if len(raw.split()) > 1:
        # has whitespace, will get quoted
        return raw
    if '"' in raw:
        # is quoted already
        return raw
    if "/" in raw or raw.endswith((":", "!", "?")):
        return f'"{raw}"'
    if raw.startswith("[") and raw.endswith("]"):
        return f'"{raw}"'
    if raw.startswith("{") and raw.endswith("}"):
        return f'"{raw}"'
    return raw


def pre_parse_query(raw: str) -> str:
    r"""
    This method does some pre-parsing of raw query strings to prepare them for
    passing on as a elasticsearch query string query (which is really just the
    lucene query language).

    Per Elasticsearch docs, the reserved characters are:

        + - = && || > < ! ( ) { } [ ] ^ " ~ * ? : \ /

    For exaple, it tries to handle trailing semi-colons (could be interpreted
    as a field filter) and slashes in words.
    """
    # if there is a fuzzy match, skip parse attempt
    # TODO: can we configure shlex to handle this?
    if '"~' in raw:
        return raw
    lex = shlex.shlex(raw, posix=False)
    lex.commenters = ""
    lex.whitespace_split = True
    tokens = list(map(_clean_token, list(lex)))
    # print(list(tokens))
    return " ".join(tokens)


def test_pre_parse_query() -> None:
    assert pre_parse_query("blah blah blah") == "blah blah blah"
    assert pre_parse_query("is_oa:") == '"is_oa:"'
    assert pre_parse_query("is_oa: ") == '"is_oa:"'
    assert pre_parse_query("is_oa:1") == "is_oa:1"
    assert pre_parse_query("is_oa:*") == "is_oa:*"
    assert pre_parse_query("<xml>") == "<xml>"
    assert pre_parse_query(r"""some $\LaTeX$""") == r"some $\LaTeX$"
    assert pre_parse_query("N/A") == '"N/A"'
    assert pre_parse_query("a/B thing") == '"a/B" thing'
    assert pre_parse_query('"a/B thing"') == '"a/B thing"'
    assert pre_parse_query("Krämer") == "Krämer"
    assert (
        pre_parse_query('"10.1093/qjmed/os-14.56.398"')
        == '"10.1093/qjmed/os-14.56.398"'
    )
    assert (
        pre_parse_query("this (is my) paper: here are the results")
        == 'this (is my) "paper:" here are the results'
    )
    assert (
        pre_parse_query('"hello world" computing type:book')
        == '"hello world" computing type:book'
    )
    assert (
        pre_parse_query('"hello world" computing type:"chapter thing"')
        == '"hello world" computing type:"chapter thing"'
    )
    assert pre_parse_query('"foo bar"~4') == '"foo bar"~4'
    assert (
        pre_parse_query("(title:foo OR title:bar)^1.5 (body:foo OR body:bar)")
        == "(title:foo OR title:bar)^1.5 (body:foo OR body:bar)"
    )
    assert (
        pre_parse_query('(title:"foo bar" AND body:"quick fox") OR title:fox')
        == '(title:"foo bar" AND body:"quick fox") OR title:fox'
    )
    assert (
        pre_parse_query("status:[400 TO 499] AND (extension:php OR extension:html)")
        == "status:[400 TO 499] AND (extension:php OR extension:html)"
    )
    assert pre_parse_query("[embargoed]") == '"[embargoed]"'
    assert (
        pre_parse_query("something 10.1002/eco.2061") == 'something "10.1002/eco.2061"'
    )
    assert pre_parse_query("different wet/dry ratios") == 'different "wet/dry" ratios'
    assert pre_parse_query("kimchy!") == '"kimchy!"'
    assert pre_parse_query("kimchy?") == '"kimchy?"'
    assert pre_parse_query("Saul B/ Cohen") == 'Saul "B/" Cohen'
    assert pre_parse_query("Nobel / Nino") == 'Nobel "/" Nino'


def sniff_citation_query(raw: str) -> bool:
    """
    This function tries to categorize raw citation strings.

    It doesn't handle lookups detection (yet? refactor?)
    """
    # if short, not citation
    if len(raw) < 12 or len(raw.split()) < 6:
        return False

    # if single quoted string, not a citation
    if raw.count('"') == 2 and raw.startswith('"') and raw.endswith('"'):
        return False

    # if there is a filter query, boost, or fuzzy match, not a citation
    if re.search(r'([a-zA-Z]:[^\s])|(["\\)][\^~]\d)', raw):
        return False

    # numbers, years, page numbers, capitalization, quoted strings all increase
    # confidence that this is a citation, not just a title
    char_types = dict()
    for c in raw:
        if c.isdigit():
            char_types["digit"] = True
        elif c >= "A" and c <= "Z":
            char_types["capitalized"] = True
        elif c == '"' or c == "'":
            char_types["quote"] = True
        elif c == ".":
            char_types["period"] = True
        elif c == ",":
            char_types["comma"] = True
        elif c == ";":
            char_types["semicolon"] = True
        elif c == "(" or c == ")":
            char_types["parens"] = True

        if len(char_types) >= 4:
            return True

    return False


def test_sniff_citation_query() -> None:
    assert sniff_citation_query("short") is False
    assert (
        sniff_citation_query("(title:foo OR title:bar)^1.5 (body:foo OR body:bar)")
        is False
    )
    assert sniff_citation_query("DR. SCHAUDINN'S WORK ON BLOOD PARASITES") is False
    assert sniff_citation_query('"DR. SCHAUDINN\'S WORK ON BLOOD PARASITES"') is False
    assert (
        sniff_citation_query(
            '"DR. SCHAUDINN\'S WORK ON BLOOD PARASITES." BMJ (Clinical Research Edition) (1905): 442-444'
        )
        is True
    )
    assert (
        sniff_citation_query(
            'Peskin, Charles S. "Numerical analysis of blood flow in the heart." Journal of computational physics 25.3 (1977): 220-252.'
        )
        is True
    )
    assert (
        sniff_citation_query(
            "Peskin, C.S., 1977. Numerical analysis of blood flow in the heart. Journal of computational physics, 25(3), pp.220-252."
        )
        is True
    )
    assert (
        sniff_citation_query(
            'Page, Don N. "Information in black hole radiation." Physical review letters 71.23 (1993): 3743.'
        )
        is True
    )
    assert (
        sniff_citation_query(
            "Hawking SW. Black hole explosions?. Nature. 1974 Mar;248(5443):30-1."
        )
        is True
    )
