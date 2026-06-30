from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from .api import (
    ApiError,
    ServerUnreachableError,
    TransientError,
    check_exists,
    make_file_context,
    make_session,
    upload,
)
from .cache import HashCache
from .config import AppConfig, ConfigError, load_config
from .crawler import FoundFile, hash_file, iter_files
from .logging_setup import setup_logging


EXIT_OK = 0
EXIT_FILE_ERRORS = 1
EXIT_CONFIG_ERROR = 2
EXIT_SERVER_UNREACHABLE = 3
EXIT_UNEXPECTED = 4

logger = logging.getLogger(__name__)


@dataclass
class RunStats:
    processed: int = 0
    uploaded: int = 0
    exists: int = 0
    skipped: int = 0
    skipped_locked: int = 0
    errors: int = 0


def run(cfg: AppConfig, *, dry_run: bool = False, limit: int | None = None, no_cache: bool = False) -> RunStats:
    stats = RunStats()
    session = make_session(cfg.server)
    cache = HashCache(
        cfg.cache.path,
        cfg.hash.algorithm,
        enabled=cfg.cache.enabled and not no_cache,
        trust_size_mtime=cfg.cache.trust_size_mtime,
    )

    try:
        for found in iter_files(cfg.crawl):
            if limit is not None and stats.processed >= limit:
                break
            stats.processed += 1
            _process_file(cfg, session, cache, found, stats, dry_run=dry_run)
    finally:
        cache.close()

    logger.info(
        "run_summary processed=%s uploaded=%s exists=%s skipped=%s skipped_locked=%s errors=%s dry_run=%s",
        stats.processed,
        stats.uploaded,
        stats.exists,
        stats.skipped,
        stats.skipped_locked,
        stats.errors,
        dry_run,
    )
    return stats


def main(argv: list[str] | None = None) -> int:
    launch_time = datetime.now(timezone.utc).isoformat()
    parser = argparse.ArgumentParser(prog="python -m doc_crawler")
    parser.add_argument("--config", required=True, help="Path to config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Hash and log planned work without API writes")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of discovered files to process")
    parser.add_argument("--no-cache", action="store_true", help="Disable the local hash cache for this run")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
        setup_logging(cfg.logging)
        logger.info("application_launched launch_time=%s config=%s", launch_time, cfg.config_path)
        logger.info("run_started config=%s", cfg.config_path)
        stats = run(cfg, dry_run=args.dry_run, limit=args.limit, no_cache=args.no_cache)
        return EXIT_FILE_ERRORS if stats.errors else EXIT_OK
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except ServerUnreachableError as exc:
        logging.getLogger(__name__).critical("server_unreachable error=%s", exc)
        return EXIT_SERVER_UNREACHABLE
    except Exception:
        logging.getLogger(__name__).exception("unexpected_failure")
        return EXIT_UNEXPECTED


def _process_file(
    cfg: AppConfig,
    session,
    cache: HashCache,
    found: FoundFile,
    stats: RunStats,
    *,
    dry_run: bool,
) -> None:
    try:
        digest = cache.get(found)
        if digest:
            logger.info("hash_cache_hit path=%s", found.path)
        else:
            digest = hash_file(found.path, cfg.hash.algorithm)
            logger.info("hashed path=%s algorithm=%s hash=%s", found.path, cfg.hash.algorithm, digest)

        file_ctx = make_file_context(cfg.server.base_url, found, digest)
        if dry_run:
            logger.info("dry_run_would_check path=%s hash=%s", found.path, digest)
            logger.info("dry_run_would_upload_if_missing path=%s", found.path)
            stats.skipped += 1
            return

        state = check_exists(cfg, session, file_ctx)
        if state == "exists":
            stats.exists += 1
            cache.put(found, digest)
            logger.info("remote_exists path=%s", found.path)
            return
        if state == "skip":
            stats.skipped += 1
            logger.info("file_rejected path=%s reason=remote_check_skipped", found.path)
            logger.warning("remote_check_skipped path=%s", found.path)
            return

        if upload(cfg, session, file_ctx, found.path):
            stats.uploaded += 1
            cache.put(found, digest)
            logger.info("uploaded path=%s", found.path)
            return
        logger.info("file_rejected path=%s reason=upload_response_rejected", found.path)
        raise ApiError("upload detection returned false")
    except PermissionError as exc:
        stats.skipped_locked += 1
        stats.errors += 1
        logger.info("file_rejected path=%s reason=locked", found.path)
        logger.warning("skipped_locked path=%s error=%s", found.path, exc)
    except OSError as exc:
        stats.errors += 1
        logger.info("file_rejected path=%s reason=file_error", found.path)
        logger.warning("file_error path=%s error=%s", found.path, exc)
    except TransientError as exc:
        if exc.kind in {"connection_error", "timeout"} and cfg.server_unreachable == "fail":
            raise ServerUnreachableError(str(exc)) from exc
        stats.errors += 1
        logger.info("file_rejected path=%s reason=transient_error kind=%s", found.path, exc.kind)
        logger.warning("transient_file_error path=%s kind=%s error=%s", found.path, exc.kind, exc)
    except ApiError as exc:
        stats.errors += 1
        logger.info("file_rejected path=%s reason=api_error", found.path)
        logger.warning("api_file_error path=%s error=%s", found.path, exc)


if __name__ == "__main__":
    raise SystemExit(main())
