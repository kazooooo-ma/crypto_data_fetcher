import backlog_v22_history_once as core

core.CODES.update({code + "0" for code in tuple(core.CODES) if len(code) == 4})

if __name__ == "__main__":
    core.main()
