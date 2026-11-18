"""The site's text pages added after the first release: /methodology and
/developers. Fixed names only: no request can name a file."""
import pathlib

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

_STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
PAGES = {"methodology": "methodology.html", "developers": "developers.html",
         "benchmarks": "benchmarks.html"}


def _page(name: str):
    async def serve():
        return HTMLResponse((_STATIC / name).read_text(encoding="utf-8"),
                            headers={"Cache-Control": "no-cache"})
    return serve


for _path, _name in PAGES.items():
    router.add_api_route(f"/{_path}", _page(_name), methods=["GET", "HEAD"], include_in_schema=False)
