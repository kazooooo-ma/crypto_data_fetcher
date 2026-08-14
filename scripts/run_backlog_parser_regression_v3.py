from pathlib import Path

import backlog_parser_regression_v2 as runner
import backlog_structured_parser_v3 as parser

runner.extract_candidates = parser.extract_candidates
runner.select_candidate = parser.select_candidate
runner.OUT = Path("out/backlog-parser-regression-v3")

if __name__ == "__main__":
    runner.main()
