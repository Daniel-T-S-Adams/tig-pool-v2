import json
cfg = json.load(open("/home/kevin/tig-master/saved_config.json"))
for s in cfg["algo_selection"]:
    settings = s.get("settings")
    aid = s["algorithm_id"]
    batch = s.get("batch_size", "?")
    print(aid.ljust(45) + "  batch=" + str(batch).rjust(4) + "  settings=" + str(settings))
