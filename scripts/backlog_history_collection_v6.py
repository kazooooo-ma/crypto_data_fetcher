import backlog_history_collection_v5 as collector
import backlog_structured_parser_v6 as parser

collector.parser = parser

if __name__ == "__main__":
    collector.base.main()
