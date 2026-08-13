from __future__ import annotations

import hashlib
import re
from typing import Any

import important_event_financing_p2_p3_once as financing
from tdnet_pdf_archive_fallback import download_record_pdf


def process(record: dict[str, str]) -> dict[str, Any]:
    cls = financing.classify(record.get("title", ""))
    out: dict[str, Any] = {**record}
    if cls is None:
        out.update(status="EXCLUDED_ROUTINE_OR_UNMATCHED")
        return out
    subtype, stage = cls
    out.update(
        event_type="FINANCING_SUPPLY",
        event_subtype=subtype,
        lifecycle_stage=stage,
        instrument_keys=financing.instrument_keys(record.get("title", "")),
    )
    blob, err, pdf_source = download_record_pdf(record)
    if not blob:
        out.update(status="PDF_DOWNLOAD_FAILED", error=err, pdf_source=pdf_source)
        return out
    text, text_err = financing.core.text_from_pdf(blob)
    out.update(
        pdf_sha256=hashlib.sha256(blob).hexdigest(),
        pdf_size=len(blob),
        text_chars=len(text),
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        pdf_source=pdf_source,
    )
    if text_err and len(re.sub(r"\s+", "", text)) < 30:
        out.update(status="PDF_TEXT_FAILED", error=text_err)
        return out
    out.update(financing.extract_fields(subtype, stage, text), status="EXTRACTED", error=text_err)
    return out


financing.process = process

if __name__ == "__main__":
    financing.main()
