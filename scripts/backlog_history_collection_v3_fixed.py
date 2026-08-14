from __future__ import annotations

from typing import Any

import backlog_history_collection_v3 as v3
import catr_backlog_source_inventory as ci


def fixed_company_inventory(item: dict[str, str]) -> dict[str, Any]:
    code = item["code"]
    company = item["company"]
    output: dict[str, Any] = {"code": code, "company": company, "details": []}
    try:
        document = ci.search_company(code, company)
        if not document:
            output["status"] = "COMPANY_NOT_FOUND"
            return output
        key = document.get("key")
        company_id = document.get("id")
        company_page = f"{ci.COMPANY_BASE}/companies/{key}/{company_id}"
        detail_url, annual_links = ci.annual_detail_url(company_page)
        output["company_page"] = company_page
        output["annual_detail_candidates"] = annual_links
        if not detail_url:
            output["status"] = "ANNUAL_DETAIL_NOT_FOUND"
            return output
        records = ci.quarter_records(detail_url, code, company)
        details: list[dict[str, Any]] = []
        for record in records:
            if not record.get("pdf_url"):
                continue
            details.append(
                {
                    **record,
                    "title": f"{record.get('fiscal_year') or ''} {record.get('quarter') or ''} 決算資料",
                    "sales_cumulative_m": record.get("sales_m"),
                    "operating_profit_cumulative_m": record.get("operating_profit_m"),
                }
            )
        details.sort(key=lambda row: (row.get("release_date") or "", row.get("quarter") or ""))
        output["details"] = details
        output["status"] = "OK" if details else "NO_QUARTER_RECORDS"
    except Exception as exc:
        output.update(status="COMPANY_INVENTORY_FAILED", error=f"{type(exc).__name__}: {exc}")
    return output


v3.company_inventory = fixed_company_inventory

if __name__ == "__main__":
    v3.main()
