"""PlatySearch CLI — init, crawl, index, serve."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="platysearch", description="PlatySearch engine CLI")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="command")

    # init
    sub.add_parser("init", help="Initialise the database")

    # crawl
    crawl_p = sub.add_parser("crawl", help="Crawl from seed URLs")
    crawl_p.add_argument("--seeds", nargs="+", required=True, help="Seed URLs to start crawling")
    crawl_p.add_argument("--max-pages", type=int, default=None, help="Max pages (overrides config)")

    # index
    sub.add_parser("index", help="(Re-)build the search index")

    # score
    sub.add_parser("score", help="Run AI-content scorer on all pages")

    # seedrefs
    seedrefs_p = sub.add_parser("seedrefs", help="Extract external reference URLs from Wikipedia pages and crawl them")
    seedrefs_p.add_argument("--max-pages", type=int, default=500, help="Max reference pages to crawl")

    # serve
    serve_p = sub.add_parser("serve", help="Start the search web server")
    serve_p.add_argument("--host", default=None)
    serve_p.add_argument("--port", type=int, default=None)

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "init":
        from platysearch.database import init_db

        asyncio.run(init_db())
        print("Database initialised.")

    elif args.command == "crawl":
        from platysearch.crawler import crawl

        count = asyncio.run(crawl(args.seeds, max_pages=args.max_pages))
        print(f"Crawled {count} pages.")

    elif args.command == "index":
        from platysearch.indexer import index_all_pages, compute_link_scores

        async def _index():
            await index_all_pages()
            await compute_link_scores()

        asyncio.run(_index())
        print("Indexing complete.")

    elif args.command == "score":
        from platysearch.ai_detector import score_all_pages

        count = asyncio.run(score_all_pages())
        print(f"Scored {count} pages.")

    elif args.command == "seedrefs":
        from platysearch.refextractor import extract_and_crawl_refs

        count = asyncio.run(extract_and_crawl_refs(max_pages=args.max_pages))
        print(f"Crawled {count} reference pages.")

    elif args.command == "serve":
        import uvicorn
        from platysearch.config import get_settings

        settings = get_settings()
        host = args.host or settings.host
        port = args.port or settings.port
        uvicorn.run("platysearch.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
