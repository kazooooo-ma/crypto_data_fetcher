import json
import traceback
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://disclosure.catr.jp"
COMPANY_BASE = "https://catr.jp"
SEARCH = BASE + "/search/typesense"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 backlog-inventory-debug-quick/1.0"})


def request(url, **kwargs):
    response = SESSION.get(url, timeout=(8, 15), **kwargs)
    return {
        "url": url,
        "status": response.status_code,
        "final_url": response.url,
        "bytes": len(response.content),
        "content_type": response.headers.get("content-type"),
        "response": response,
    }


def main():
    out = {"code": "1434", "company": "JESCOホールディングス", "steps": []}
    try:
        params = {
            "q": "1434",
            "query_by": "full_text_search",
            "infix": "always",
            "num_typos": "0",
            "prefix": "false",
            "per_page": 20,
            "page": 1,
            "sort_by": "settlement_count:desc",
        }
        step = request(SEARCH, params=params)
        response = step.pop("response")
        out["steps"].append(step)
        data = response.json()
        docs = [hit.get("document") or {} for hit in data.get("hits") or []]
        out["search_documents"] = docs
        exact = [doc for doc in docs if str(doc.get("ticker_code") or "") == "1434"]
        if not exact:
            out["status"] = "SEARCH_NO_EXACT"
        else:
            doc = exact[0]
            key, company_id = doc.get("key"), doc.get("id")
            company_page = f"{COMPANY_BASE}/companies/{key}/{company_id}"
            step = request(company_page)
            response = step.pop("response")
            out["steps"].append(step)
            soup = BeautifulSoup(response.text, "html.parser")
            links = []
            for anchor in soup.find_all("a", href=True):
                href = urljoin(response.url, anchor["href"])
                if href.startswith(BASE) and "/tdnet/" in href:
                    links.append(href)
            out["company_links"] = list(dict.fromkeys(links))[:20]
            if not links:
                out["status"] = "NO_TDNET_LINKS_ON_COMPANY_PAGE"
            else:
                detail_url = links[0]
                step = request(detail_url)
                response = step.pop("response")
                out["steps"].append(step)
                soup = BeautifulSoup(response.text, "html.parser")
                out["detail_title"] = (soup.find("h2") or soup.find("h1")).get_text(" ", strip=True) if (soup.find("h2") or soup.find("h1")) else None
                tables = []
                for table in soup.find_all("table"):
                    headers = [cell.get_text(" ", strip=True) for cell in table.find_all("th")]
                    if headers:
                        tables.append(headers[:20])
                out["table_headers"] = tables[:20]
                out["status"] = "OK"
    except Exception as exc:
        out["status"] = "EXCEPTION"
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["traceback"] = traceback.format_exc()
    with open("inventory-debug-quick.json", "w", encoding="utf-8") as handle:
        json.dump(out, handle, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
