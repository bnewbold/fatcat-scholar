import typing
import jinja2

from starlette.background import BackgroundTask
from starlette.templating import _TemplateResponse


class Jinja2Templates:
    """
    This is a patched version of starlette.templating.Jinja2Templates that
    supports extensions (list of strings) passed to jinja2.Environment
    """

    def __init__(self, directory: str, extensions: typing.List[str] = []) -> None:
        assert jinja2 is not None, "jinja2 must be installed to use Jinja2Templates"
        self.env = self.get_env(directory, extensions)

    def get_env(
        self, directory: str, extensions: typing.List[str] = []
    ) -> "jinja2.Environment":
        @jinja2.contextfunction
        def url_for(context: dict, name: str, **path_params: typing.Any) -> str:
            request = context["request"]
            return request.url_for(name, **path_params)

        loader = jinja2.FileSystemLoader(directory)
        env = jinja2.Environment(loader=loader, extensions=extensions, autoescape=True)
        env.globals["url_for"] = url_for
        return env

    def get_template(self, name: str) -> "jinja2.Template":
        return self.env.get_template(name)

    def TemplateResponse(
        self,
        name: str,
        context: dict,
        status_code: int = 200,
        headers: dict = None,
        media_type: str = None,
        background: BackgroundTask = None,
    ) -> _TemplateResponse:
        if "request" not in context:
            raise ValueError('context must include a "request" key')
        template = self.get_template(name)
        return _TemplateResponse(
            template,
            context,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
            background=background,
        )


def parse_accept_lang(header: str, options: typing.List[str]) -> typing.Optional[str]:
    """
    Crude HTTP Accept-Language content negotiation.
    Assumes that languages are specified in order of priority, etc.
    """
    if not header:
        return None
    chunks = [v.split(";")[0].split("-")[0].split("_")[0] for v in header.split(",")]
    for c in chunks:
        if len(c) == 2 and c in options:
            return c
    return None


def test_parse_accept_lang() -> None:
    assert parse_accept_lang("", []) is None
    assert parse_accept_lang("en,de", []) is None
    assert parse_accept_lang("en,de", ["en"]) == "en"
    assert parse_accept_lang("en-GB,de", ["en"]) == "en"
    assert parse_accept_lang("zh_Hans_CN", ["en", "zh"]) == "zh"
    assert parse_accept_lang("en,de", ["de"]) == "de"
    assert (
        parse_accept_lang("en-ca,en;q=0.8,en-us;q=0.6,de-de;q=0.4,de;q=0.2", ["de"])
        == "de"
    )
    assert (
        parse_accept_lang(
            "en-ca,en;q=0.8,en-us;q=0.6,de-de;q=0.4,de;q=0.2", ["en", "de"]
        )
        == "en"
    )
    assert (
        parse_accept_lang("en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7", ["zh", "en", "de"])
        == "en"
    )
