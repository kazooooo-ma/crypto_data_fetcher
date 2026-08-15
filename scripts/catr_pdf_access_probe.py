from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

OUT = Path("out/catr-pdf-access-probe")
PDF = "https://pdf.catr.jp/2021/01/0000321430_a6bfc1e5cebbe0b384506a77ac37af34.pdf"
DETAIL = "https://disclosure.catr.jp/companies/a6384/42410/tdnet/e48b2/321430"


def record_response(name: str, response: requests.Response) -> dict:
    payload = response.content
    return {
        "name": name,
        "url": response.url,
        "status": response.status_code,
        "headers": dict(response.headers),
        "bytes": len(payload),
        "prefix_hex": payload[:32].hex(),
        "prefix_text": payload[:300].decode("utf-8", errors="replace"),
        "cookies": response.cookies.get_dict(),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    variants = [
        ("default", PDF, {}),
        (
            "browser",
            PDF,
            {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36",
                "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
                "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
                "Accept-Encoding": "identity",
                "Referer": "https://catr.jp/",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-site",
                "Upgrade-Insecure-Requests": "1",
            },
        ),
        ("detail_referer", PDF, {"User-Agent": "Mozilla/5.0", "Referer": DETAIL, "Accept": "application/pdf,*/*"}),
        ("range", PDF, {"User-Agent": "Mozilla/5.0", "Referer": DETAIL, "Range": "bytes=0-1048575", "Accept-Encoding": "identity"}),
        ("download_query", PDF + "?download=1", {"User-Agent": "Mozilla/5.0", "Referer": DETAIL}),
        ("raw_query", PDF + "?raw=1", {"User-Agent": "Mozilla/5.0", "Referer": DETAIL}),
        ("dl_query", PDF + "?dl=1", {"User-Agent": "Mozilla/5.0", "Referer": DETAIL}),
    ]
    results = []
    for name, url, headers in variants:
        try:
            response = session.get(url, headers=headers, timeout=45, allow_redirects=True)
            results.append(record_response(name, response))
        except Exception as exc:
            results.append({"name": name, "error": f"{type(exc).__name__}: {exc}"})

    detail_variants = [
        ("detail_disclosure", DETAIL),
        ("detail_catr", DETAIL.replace("https://disclosure.catr.jp", "https://catr.jp")),
    ]
    for name, url in detail_variants:
        try:
            response = session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=45, allow_redirects=True)
            item = record_response(name, response)
            soup = BeautifulSoup(response.text, "html.parser")
            links = []
            for tag in soup.find_all(["a", "iframe", "embed", "object", "script", "link", "meta"]):
                value = tag.get("href") or tag.get("src") or tag.get("data") or tag.get("content")
                if value:
                    links.append(urljoin(response.url, value))
            regex_urls = re.findall(r"https?://[^\"'<>\\s]+", response.text)
            item["links"] = list(dict.fromkeys(links + regex_urls))[:300]
            item["html_excerpt"] = response.text[:20000]
            results.append(item)
        except Exception as exc:
            results.append({"name": name, "error": f"{type(exc).__name__}: {exc}"})

    commands = {
        "curl_http1": ["curl", "--http1.1", "-L", "--compressed", "-A", "Mozilla/5.0", "-e", DETAIL, "-D", "-", "-o", str(OUT / "curl-http1.bin"), PDF],
        "curl_range": ["curl", "--http1.1", "-L", "-A", "Mozilla/5.0", "-e", DETAIL, "-H", "Range: bytes=0-1048575", "-D", "-", "-o", str(OUT / "curl-range.bin"), PDF],
        "wget": ["wget", "--server-response", "--user-agent=Mozilla/5.0", f"--referer={DETAIL}", "-O", str(OUT / "wget.bin"), PDF],
    }
    for name, command in commands.items():
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=90)
            target = OUT / ("curl-http1.bin" if name == "curl_http1" else "curl-range.bin" if name == "curl_range" else "wget.bin")
            results.append({
                "name": name,
                "returncode": completed.returncode,
                "stdout": completed.stdout[-10000:],
                "stderr": completed.stderr[-10000:],
                "bytes": target.stat().st_size if target.exists() else None,
                "prefix_hex": target.read_bytes()[:32].hex() if target.exists() else None,
            })
        except Exception as exc:
            results.append({"name": name, "error": f"{type(exc).__name__}: {exc}"})

    (OUT / "probe.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
