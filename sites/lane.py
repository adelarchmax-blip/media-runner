"""Site jobs: the controller writes them down, n8n hands them out, the runner (sites.yml) does them here.

Each job comes through the `miinspact-site` webhook and its answer goes back the same way:
  next   {kinds, run_id}            -> {job: {id, kind, work}} | {job: None}
  beat   {ids, run_id}              every BEAT seconds: these jobs are still alive
  seen   {id, urls, run_id}         -> {known: [...]}: pages handled before (the full memory)
  part   {id, part, run_id}         finds saved while the scan goes on
  done   {id, result | error, run_id}
Kinds:
  torrent_scan     one torrent site's scan, in its own Chrome tab   -> sites.torrent_scan.scan
  torrent_explore  one torrent site's sections map, in its own tab  -> sites.torrent_scan.explore
  torrent_search   missing seasons / film parts, by the site's search -> sites.torrent_scan.scan (queries)
  watch_explore    one watch site's sections map (address checked first) -> sites.watch.explore
  watch_crawl      one watch site's new items, section by section        -> sites.watch.crawl
  life             whether anyone shares queued torrents            -> sites.health.check (no browser)
  page_info        a page added by hand: its real title and poster  -> sites.explorer.page_identity
  title_info       posters, seasons, film series (free databases)   -> sites.title_info.lookup (no browser)
  tg_scan          one Telegram channel's new messages               -> sites.telegram (no browser)
  tg_join          joining a private Telegram channel from its invite -> sites.telegram (no browser)
Telegram jobs are asked for only when the account is set up (the `telegram` secrets), one at a time.
Up to PARALLEL sites are read side by side while the light jobs (no browser) run beside them. Chrome starts only when a
job needs it. With nothing left, the runner waits IDLE_MINUTES for new jobs before it ends, so a job added
meanwhile starts at once. Logs carry kinds and counts only, never an address.
"""
import concurrent.futures
import logging
import threading
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from sites import explorer, health, telegram, title_info, torrent_files, torrent_scan, watch

HOOK = "miinspact-site"
MAX_MINUTES = 330              # the run's own limit (sites.yml stops at 340)
IDLE_MINUTES = 10              # waiting for new jobs after the last one
POLL = 30                      # seconds between questions while waiting
BEAT = 60                      # seconds between heartbeats
PARALLEL = 3                   # sites read at the same time (one tab each)
BROWSER_KINDS = ("torrent_scan", "torrent_search", "torrent_explore", "watch_explore", "watch_crawl", "page_info")
LIGHT_KINDS = ("life", "title_info")
TG_KINDS = ("tg_scan", "tg_join")


def _page_info(work: dict, fetch) -> dict:
    url = str(work.get("url") or "")
    if not url.startswith(("http://", "https://")):
        raise ValueError("bad address")
    _status, final, html = fetch(url)
    title, image = explorer.page_identity(BeautifulSoup(html or "", "html.parser"))
    poster = urljoin(final or url, image) if image else None
    return {"title": title, "poster": poster if poster and poster.startswith(("http://", "https://")) else None}


def _life(work: dict) -> dict:
    items = [i for i in (work.get("items") or []) if isinstance(i, dict) and isinstance(i.get("id"), int)]

    def one(item):
        try:
            res = health.check(torrent_files.parse_magnet(str(item.get("magnet") or "")))
        except torrent_files.TorrentError:
            res = {"state": "dead", "seeds": 0, "peers": 0}
        except Exception as e:  # one bad answer must not lose the rest
            logging.info(f"life check: {type(e).__name__}")
            res = {"state": "unknown", "seeds": None, "peers": None}
        return dict(res, id=item["id"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=health.WORKERS) as ex:
        return {"results": list(ex.map(one, items))}


class _Run:
    def __init__(self, api, run_id: str):
        self.api, self.run_id = api, run_id
        self.lock = threading.Lock()
        self.active = set()

    def ask(self, op: str, **data) -> dict:
        return self.api(HOOK, dict(data, op=op, run_id=self.run_id)) or {}

    def beat_loop(self, stop: threading.Event):
        while not stop.wait(BEAT):
            with self.lock:
                ids = sorted(self.active)
            if ids:
                self.ask("beat", ids=ids)

    def seen_for(self, job_id: int):
        def ask(urls):
            got = self.ask("seen", id=job_id, urls=urls)
            if not isinstance(got.get("known"), list):
                raise RuntimeError("no answer")
            return got["known"]
        return ask

    def part_for(self, job_id: int):
        def send(part):
            if not self.ask("part", id=job_id, part=part).get("ok"):
                raise RuntimeError("not saved")
        return send

    def do(self, job: dict, chrome) -> None:
        kind, work, started = job.get("kind"), job.get("work") or {}, time.time()
        try:
            if kind == "life":
                result = _life(work)
            elif kind == "title_info":
                result = title_info.lookup(work)
            elif kind == "tg_scan":
                result = telegram.client().scan(work)
            elif kind == "tg_join":
                result = telegram.client().join(work)
            elif kind in BROWSER_KINDS:
                with chrome.tab() as tab:
                    if kind in ("torrent_scan", "torrent_search"):
                        result = torrent_scan.scan(work, tab.fetch, tab.get_bytes,
                                                   memory=torrent_scan.Memory(self.seen_for(job["id"])),
                                                   send_part=self.part_for(job["id"]))
                    elif kind == "watch_crawl":
                        result = watch.crawl(work, tab.fetch, torrent_scan.Memory(self.seen_for(job["id"])),
                                             self.part_for(job["id"]))
                    elif kind == "watch_explore":
                        result = watch.explore(work, tab.fetch)
                    elif kind == "page_info":
                        result = _page_info(work, tab.fetch)
                    else:
                        result = {"profile": torrent_scan.explore(work, tab.fetch)}
            else:
                raise ValueError("unknown kind")
            answer = {"id": job["id"], "result": result}
        except Exception as e:  # the controller records the failure; the other jobs go on
            logging.info(f"site job ({kind}) failed: {type(e).__name__}")
            answer = {"id": job["id"], "error": f"{type(e).__name__}"}
        self.ask("done", **answer)
        with self.lock:
            self.active.discard(job["id"])
        logging.info(f"site job ({kind}) done in {time.time() - started:.0f}s")


def run(api, sessions_file: str = "", browser_cls=None, minutes: float = MAX_MINUTES,
        idle_minutes: float = IDLE_MINUTES, run_id: str = "", parallel: int = PARALLEL, poll: float = POLL) -> int:
    """Does site jobs until none came for `idle_minutes` or the time is up. Returns how many were done."""
    if browser_cls is None:
        from sites.browser import Chrome as browser_cls
    end = time.time() + minutes * 60
    run = _Run(api, run_id)
    stop = threading.Event()
    threading.Thread(target=run.beat_loop, args=(stop,), daemon=True).start()
    pages = concurrent.futures.ThreadPoolExecutor(max_workers=parallel)
    lights = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    tg = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    busy = {"browser": set(), "light": set(), "tg": set()}
    done, chrome, last_work = 0, None, time.time()
    try:
        while True:
            for lane in busy.values():
                finished = {f for f in lane if f.done()}
                done += len(finished)
                lane -= finished
            kinds = (list(BROWSER_KINDS) if len(busy["browser"]) < parallel else []) + \
                (list(LIGHT_KINDS) if not busy["light"] else []) + \
                (list(TG_KINDS) if telegram.configured() and not busy["tg"] else [])
            job = None
            if kinds and time.time() < end:
                job = run.ask("next", kinds=kinds).get("job")
            if isinstance(job, dict) and isinstance(job.get("id"), int):
                last_work = time.time()
                with run.lock:
                    run.active.add(job["id"])
                if job.get("kind") in BROWSER_KINDS and chrome is None:
                    try:
                        chrome = browser_cls(sessions_file).__enter__()
                    except Exception as e:  # no Chrome: this job fails, the light jobs still run
                        logging.info(f"sites browser not started: {type(e).__name__}")
                        run.ask("done", id=job["id"], error=type(e).__name__)
                        with run.lock:
                            run.active.discard(job["id"])
                        done += 1
                        continue
                lane = "light" if job.get("kind") in LIGHT_KINDS else "tg" if job.get("kind") in TG_KINDS else "browser"
                busy[lane].add({"light": lights, "tg": tg, "browser": pages}[lane].submit(run.do, job, chrome))
                continue
            running = busy["browser"] | busy["light"] | busy["tg"]
            if not running and (time.time() >= end or time.time() - last_work >= idle_minutes * 60):
                break
            if running:
                concurrent.futures.wait(running, timeout=poll, return_when=concurrent.futures.FIRST_COMPLETED)
                last_work = time.time()
            else:
                time.sleep(min(poll, max(0.0, end - time.time())))
    finally:
        pages.shutdown(wait=True)
        lights.shutdown(wait=True)
        tg.shutdown(wait=True)
        telegram.close()
        done += sum(len(lane) for lane in busy.values())
        stop.set()
        if chrome is not None:
            chrome.__exit__(None, None, None)
    return done
