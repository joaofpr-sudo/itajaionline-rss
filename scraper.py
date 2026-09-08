import html
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://itajaionline.com.br"
LIST_URL = f"{BASE}/vagas"
OUT = Path("vagas.xml")
STATE = Path("state.json")
MAX_ITEMS = 100
MAX_LIST_PAGES = 3
TIMEOUT = 25
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ItajaiOnlineRSS/1.0; +https://github.com/joaofpr-sudo/itajaionline-rss)"
}

session = requests.Session()
session.headers.update(HEADERS)


def clean(text):
    text = html.unescape(text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def absolute(url):
    return urljoin(BASE, url)


def is_job_url(url):
    path = urlparse(url).path.rstrip("/")
    return bool(re.fullmatch(r"/vaga/\d+-[^/]+", path))


def extract_list_page(url):
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    jobs = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = absolute(a["href"])
        if not is_job_url(href) or href in seen:
            continue
        seen.add(href)
        title = clean(a.get_text(" ", strip=True))
        if title:
            jobs.append({"url": href, "list_title": title})
    next_pages = []
    for a in soup.find_all("a", href=True):
        href = absolute(a["href"])
        if re.search(r"/vagas/parte-\d+$", urlparse(href).path) and href not in next_pages:
            next_pages.append(href)
    return jobs, next_pages


def extract_detail(job):
    r = session.get(job["url"], timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    page_title = clean(soup.title.get_text()) if soup.title else ""

    title = job["list_title"]
    city = ""
    description = ""

    # The page title follows: Vaga: NOME Cidade: CIDADE | Itajaí Online
    m = re.match(r"Vaga:\s*(.*?)\s+Cidade:\s*(.*?)\s*\|\s*Itajaí Online\s*$", page_title, re.I)
    if m:
        title = clean(m.group(1)) or title
        city = clean(m.group(2)).rstrip("-").strip()

    body = soup.get_text("\n", strip=True)
    lines = [clean(x) for x in body.splitlines() if clean(x)]

    if not city:
        for line in lines:
            m = re.match(r"Cidade:\s*(.+)$", line, re.I)
            if m:
                city = clean(m.group(1)).rstrip("-").strip()
                break

    # On the current site the main content is title, city, description.
    if city:
        for i, line in enumerate(lines):
            if line.rstrip("-").strip().casefold() == city.casefold() and i + 1 < len(lines):
                candidate = clean(lines[i + 1])
                if candidate and candidate.casefold() not in {"hoje", "ontem"}:
                    description = candidate
                    break

    if not description:
        # Fallback: visible paragraph text, excluding navigation/footer boilerplate.
        for p in soup.find_all("p"):
            text = clean(p.get_text(" ", strip=True))
            if text and len(text) > 20 and "Itajaí Online" not in text:
                description = text
                break

    job["title"] = title or job["list_title"]
    job["city"] = city or ""
    job["description"] = description or "Descrição não informada."
    job["published_at"] = datetime.now(timezone.utc).isoformat()
    return job


def load_state():
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def parse_date(value):
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except Exception:
        try:
            return parsedate_to_datetime(value).astimezone(timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)


def build_rss(items):
    now = datetime.now(timezone.utc)
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Vagas de Emprego — Itajaí Online"
    ET.SubElement(channel, "link").text = LIST_URL
    ET.SubElement(channel, "description").text = "Vagas de emprego do Itajaí Online, com cargo, cidade e descrição."
    ET.SubElement(channel, "language").text = "pt-BR"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(now)

    for job in items[:MAX_ITEMS]:
        item = ET.SubElement(channel, "item")
        title = job["title"]
        if job.get("city"):
            title = f"{title} — {job['city']}"
        ET.SubElement(item, "title").text = title
        ET.SubElement(item, "link").text = job["url"]
        ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = job["url"]
        ET.SubElement(item, "description").text = job["description"]
        if job.get("city"):
            ET.SubElement(item, "category").text = job["city"]
        ET.SubElement(item, "pubDate").text = format_datetime(parse_date(job.get("published_at")))

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    tree.write(OUT, encoding="utf-8", xml_declaration=True)


def main():
    state = load_state()
    jobs = []
    seen = set()
    queue = [LIST_URL]
    visited = set()

    while queue and len(visited) < MAX_LIST_PAGES and len(jobs) < MAX_ITEMS:
        page = queue.pop(0)
        if page in visited:
            continue
        visited.add(page)
        try:
            found, next_pages = extract_list_page(page)
        except Exception as exc:
            print(f"Falha ao ler {page}: {exc}")
            continue
        for job in found:
            if job["url"] not in seen:
                seen.add(job["url"])
                jobs.append(job)
                if len(jobs) >= MAX_ITEMS:
                    break
        for nxt in next_pages:
            if nxt not in visited and nxt not in queue:
                queue.append(nxt)

    result = []
    for index, job in enumerate(jobs):
        url = job["url"]
        if url in state:
            result.append(state[url])
            continue
        try:
            print(f"Nova vaga {index + 1}/{len(jobs)}: {url}")
            result.append(extract_detail(job))
            time.sleep(0.15)
        except Exception as exc:
            print(f"Falha no detalhe {url}: {exc}")
            job["title"] = job["list_title"]
            job["city"] = ""
            job["description"] = "Abra a vaga para ver a descrição completa."
            job["published_at"] = datetime.now(timezone.utc).isoformat()
            result.append(job)

    current_urls = {x["url"] for x in result}
    merged = {j["url"]: j for j in result}
    for url, job in state.items():
        merged.setdefault(url, job)

    ordered = result + [j for u, j in merged.items() if u not in current_urls]
    ordered = ordered[:MAX_ITEMS]

    new_state = {j["url"]: j for j in ordered}
    STATE.write_text(json.dumps(new_state, ensure_ascii=False, indent=2), encoding="utf-8")
    build_rss(ordered)
    print(f"RSS atualizado com {len(ordered)} vagas.")


if __name__ == "__main__":
    main()
