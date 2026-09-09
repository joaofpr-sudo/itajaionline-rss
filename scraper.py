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
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ItajaiOnlineRSS/1.0)"}

# Pesos maiores = maior prioridade no feed.
# Financeiro vem antes de Administrativo; dentro de cada grupo,
# termos específicos pesam mais do que termos genéricos.
KEYWORDS = {
    # FINANCEIRO — prioridade máxima
    "faturamento": 120, "faturacao": 120, "financeiro": 120, "financeira": 120,
    "contas a pagar": 120, "contas a receber": 120, "contas a pagar e receber": 125,
    "accounts payable": 115, "accounts receivable": 115, "billing": 110,
    "analista financeiro": 115, "assistente financeiro": 115, "auxiliar financeiro": 110,
    "coordenador financeiro": 115, "supervisor financeiro": 115, "gerente financeiro": 115,
    "cobrança": 100, "cobranca": 100, "tesouraria": 100,
    "conciliação bancária": 100, "conciliacao bancaria": 100,
    "crédito e cobrança": 95, "credito e cobranca": 95,
    "controladoria": 90, "contabilidade": 90, "contábil": 90, "contabil": 90,
    "fiscal": 70, "billing analyst": 105, "billing assistant": 105,
    "finance analyst": 105, "finance assistant": 105,

    # ADMINISTRATIVO — segunda prioridade
    "administrativo": 85, "administrativa": 85,
    "analista administrativo": 95, "assistente administrativo": 95, "auxiliar administrativo": 90,
    "coordenador administrativo": 95, "supervisor administrativo": 95, "gerente administrativo": 95,
    "rotinas administrativas": 85, "rotina administrativa": 85,
    "apoio administrativo": 85, "setor administrativo": 85, "área administrativa": 85,
    "backoffice": 80, "back office": 80, "secretária": 70, "secretaria": 70,
    "recepção administrativa": 70, "recepcao administrativa": 70,
    "compras": 70, "suprimentos": 70, "departamento pessoal": 75,
    "recursos humanos": 65, "rh": 55,
}

session = requests.Session()
session.headers.update(HEADERS)


def clean(text):
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def absolute(url):
    return urljoin(BASE, url)


def is_job_url(url):
    return bool(re.fullmatch(r"/vaga/\d+-[^/]+", urlparse(url).path.rstrip("/")))


def classify(job):
    text = clean(" ".join([job.get("title", ""), job.get("list_title", ""), job.get("description", "")])).casefold()
    finance = []
    admin = []
    for word, points in KEYWORDS.items():
        if word.casefold() in text:
            (finance if points >= 100 else admin).append((word, points))
    finance_score = sum(p for _, p in finance)
    admin_score = sum(p for _, p in admin)
    if finance_score:
        return finance_score, "FINANCEIRO", [w for w, _ in sorted(finance, key=lambda x: -x[1])]
    if admin_score:
        return admin_score, "ADMINISTRATIVO", [w for w, _ in sorted(admin, key=lambda x: -x[1])]
    return 0, "OUTRAS", []


def extract_list_page(url):
    r = session.get(url, timeout=TIMEOUT); r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    jobs, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = absolute(a["href"])
        if is_job_url(href) and href not in seen:
            seen.add(href)
            title = clean(a.get_text(" ", strip=True))
            if title: jobs.append({"url": href, "list_title": title})
    next_pages = []
    for a in soup.find_all("a", href=True):
        href = absolute(a["href"])
        if re.search(r"/vagas/parte-\d+$", urlparse(href).path) and href not in next_pages:
            next_pages.append(href)
    return jobs, next_pages


def extract_detail(job):
    r = session.get(job["url"], timeout=TIMEOUT); r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    page_title = clean(soup.title.get_text()) if soup.title else ""
    title, city, description = job["list_title"], "", ""
    m = re.match(r"Vaga:\s*(.*?)\s+Cidade:\s*(.*?)\s*\|\s*Itajaí Online\s*$", page_title, re.I)
    if m:
        title, city = clean(m.group(1)), clean(m.group(2)).rstrip("-").strip()
    lines = [clean(x) for x in soup.get_text("\n", strip=True).splitlines() if clean(x)]
    if not city:
        for line in lines:
            m = re.match(r"Cidade:\s*(.+)$", line, re.I)
            if m: city = clean(m.group(1)).rstrip("-").strip(); break
    if city:
        for i, line in enumerate(lines):
            if line.rstrip("-").strip().casefold() == city.casefold() and i + 1 < len(lines):
                candidate = clean(lines[i + 1])
                if candidate.casefold() not in {"hoje", "ontem"} and len(candidate) > 10:
                    description = candidate; break
    if not description:
        for p in soup.find_all("p"):
            text = clean(p.get_text(" ", strip=True))
            if len(text) > 20 and "Itajaí Online" not in text:
                description = text; break
    job.update(title=title or job["list_title"], city=city, description=description or "Descrição não informada.")
    job["published_at"] = job.get("published_at") or datetime.now(timezone.utc).isoformat()
    score, category, matches = classify(job)
    job.update(priority_score=score, category=category, matches=matches)
    return job


def load_state():
    try: return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    except Exception: return {}


def parse_date(value):
    try: return datetime.fromisoformat(value).astimezone(timezone.utc)
    except Exception:
        try: return parsedate_to_datetime(value).astimezone(timezone.utc)
        except Exception: return datetime.now(timezone.utc)


def build_rss(items):
    now = datetime.now(timezone.utc)
    rss = ET.Element("rss", {"version": "2.0"})
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = "Vagas — Itajaí Online — Financeiro e Administrativo"
    ET.SubElement(ch, "link").text = LIST_URL
    ET.SubElement(ch, "description").text = "Feed de vagas com prioridade para Financeiro, Faturamento, Contas a Pagar/Receber e Administrativo."
    ET.SubElement(ch, "language").text = "pt-BR"
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(now)
    for job in items[:MAX_ITEMS]:
        prefix = {"FINANCEIRO": "★ FINANCEIRO", "ADMINISTRATIVO": "☆ ADMINISTRATIVO"}.get(job.get("category"), "")
        title = f"{prefix} | " if prefix else ""
        title += job["title"] + (f" — {job['city']}" if job.get("city") else "")
        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text = title
        ET.SubElement(item, "link").text = job["url"]
        ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = job["url"]
        desc = job["description"]
        if job.get("category") != "OUTRAS":
            desc = f"[{job['category']}] " + desc
            if job.get("matches"): desc += " | Termos identificados: " + ", ".join(job["matches"][:6])
        ET.SubElement(item, "description").text = desc
        ET.SubElement(item, "category").text = job.get("category", "OUTRAS")
        ET.SubElement(item, "pubDate").text = format_datetime(parse_date(job.get("published_at")))
    tree = ET.ElementTree(rss); ET.indent(tree, space="  "); tree.write(OUT, encoding="utf-8", xml_declaration=True)


def main():
    state = load_state(); jobs=[]; seen=set(); queue=[LIST_URL]; visited=set()
    while queue and len(visited) < MAX_LIST_PAGES and len(jobs) < MAX_ITEMS:
        page=queue.pop(0)
        if page in visited: continue
        visited.add(page)
        try: found, nxt = extract_list_page(page)
        except Exception as e: print(f"Falha {page}: {e}"); continue
        for j in found:
            if j["url"] not in seen:
                seen.add(j["url"]); jobs.append(j)
                if len(jobs)>=MAX_ITEMS: break
        for n in nxt:
            if n not in visited and n not in queue: queue.append(n)
    result=[]
    for j in jobs:
        if j["url"] in state:
            # Reclassifica inclusive vagas antigas para que a nova regra seja aplicada.
            old=state[j["url"]]; score,cat,matches=classify(old)
            old.update(priority_score=score, category=cat, matches=matches)
            result.append(old); continue
        try: result.append(extract_detail(j)); time.sleep(0.15)
        except Exception as e:
            print(f"Falha detalhe {j['url']}: {e}")
            j.update(title=j["list_title"], city="", description="Abra a vaga para ver a descrição completa.", published_at=datetime.now(timezone.utc).isoformat())
            score,cat,matches=classify(j); j.update(priority_score=score,category=cat,matches=matches); result.append(j)
    # Preserva histórico e coloca primeiro Financeiro, depois Administrativo, depois demais.
    merged={j["url"]:j for j in result}
    for u,j in state.items():
        if u not in merged:
            score,cat,matches=classify(j); j.update(priority_score=score,category=cat,matches=matches); merged[u]=j
    ordered=list(merged.values())
    ordered.sort(key=lambda j:(j.get("priority_score",0), parse_date(j.get("published_at"))), reverse=True)
    ordered=ordered[:MAX_ITEMS]
    STATE.write_text(json.dumps({j["url"]:j for j in ordered}, ensure_ascii=False, indent=2), encoding="utf-8")
    build_rss(ordered)

if __name__ == "__main__": main()
