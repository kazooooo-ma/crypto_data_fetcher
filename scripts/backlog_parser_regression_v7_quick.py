from pathlib import Path

import backlog_parser_regression_v6_quick as base
from backlog_structured_parser_v7 import extract_candidates, select_candidate

base.extract_candidates = extract_candidates
base.select_candidate = select_candidate
base.OUT = Path("out/backlog-parser-regression-v7-quick")

if __name__ == "__main__":
    base.main()
