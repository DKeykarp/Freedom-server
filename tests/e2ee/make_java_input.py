import json
js = json.load(open("/tmp/js_out.json"))
lines = [f"iters={js['iters']}", f"bobpw={js['users']['bob']['pw']}", f"bobauth={js['users']['bob']['auth']}",
  f"bobblob={js['users']['bob']['blob']}", f"alicedh={js['users']['alice']['dh']}", f"alicesig={js['users']['alice']['sig']}",
  f"bobdh={js['users']['bob']['dh']}", f"bobsig={js['users']['bob']['sig']}",
  f"env_eph={js['envelopes'][1]['eph']}", f"env_n={js['envelopes'][1]['n']}", f"env_c={js['envelopes'][1]['c']}", f"env_sig={js['envelopes'][1]['sig']}",
  f"K={js['K']}", f"msg_ct={js['msg']['ct']}", f"msg_sig={js['msg']['sig']}",
  f"file_enc={js['file']['enc']}", f"file_key={js['file']['key']}", f"file_nonce={js['file']['nonce']}", f"file_sha={js['file']['sha']}",
  f"signal_n={js['signal']['n']}", f"signal_c={js['signal']['c']}", f"safety={js['safety']}"]
open("/tmp/for_java.txt", "w", encoding="utf-8").write("\n".join(lines))
