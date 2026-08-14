from pathlib import Path

import backlog_parser_regression_v3 as base
from backlog_structured_parser_v4 import extract_candidates, select_candidate

base.extract_candidates = extract_candidates
base.select_candidate = select_candidate
base.base.extract_candidates = extract_candidates
base.base.select_candidate = select_candidate
base.base.OUT = Path("out/backlog-parser-regression-v4")
base.OUT = base.base.OUT

if __name__ == "__main__":
    base.main()
