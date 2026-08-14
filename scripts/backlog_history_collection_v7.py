from __future__ import annotations

import re
import unicodedata

import backlog_history_collection_v5 as collector
import backlog_structured_parser_v7 as parser


def mixed_money_to_million(text: str | None) -> float | None:
    if not text:
        return None
    normalized = unicodedata.normalize("NFKC", text).replace(",", "").replace(" ", "")
    units = {
        "兆円": 1_000_000.0,
        "億円": 100.0,
        "億": 100.0,
        "百万円": 1.0,
        "千円": 0.001,
        "万円": 0.01,
        "円": 0.000001,
    }
    matches = list(
        re.finditer(
            r"([0-9]+(?:\.[0-9]+)?)(兆円|億円|億|百万円|千円|万円|円)",
            normalized,
        )
    )
    if not matches:
        return None
    return sum(float(match.group(1)) * units[match.group(2)] for match in matches)


collector.parser = parser
collector.inventory.money_to_million = mixed_money_to_million

if __name__ == "__main__":
    collector.base.main()
