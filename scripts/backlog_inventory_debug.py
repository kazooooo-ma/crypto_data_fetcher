import json
import traceback

import backlog_history_collection_v5 as pipeline


def main():
    item = {"code": "1434", "company": "JESCOホールディングス"}
    try:
        result = pipeline.fixed_company_inventory(item)
    except Exception as exc:
        result = {
            "status": "UNCAUGHT_EXCEPTION",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    with open("inventory-debug.json", "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
