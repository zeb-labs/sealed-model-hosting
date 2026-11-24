#!/bin/bash
# Runs once at boot on the test client (stands for the customer's app).
# -k: self-signed certificate; its fingerprint is printed and compared later.
T=https://TARGET
J='Content-Type: application/json'; AU='Authorization: Bearer APIKEY'
say() { echo "SMH CLIENT $(date -u +%H:%M:%S) $*" > /dev/console; }
code() { curl -s -k -m 60 --path-as-is -H "$AU" -o /tmp/body -w '%{http_code}' "$@"; }

for _ in $(seq 1 160); do
  c=$(code $T/health); say "health=$c"; [ "$c" = 200 ] && break; sleep 15
done

# Port scan: only the front gate may answer.
H=${T#https://}
say "cert $(echo | openssl s_client -connect $H:443 2>/dev/null | openssl x509 -noout -fingerprint -sha256 | cut -d= -f2 | tr -d :)"
say "nokey -> $(curl -s -k -m 20 -o /dev/null -w '%{http_code}' -H "$J" -d '{"model":"demo","prompt":"Hi","max_tokens":2}' $T/v1/completions)"
for p in 22 23 80 443 2375 2376 3389 5900 8000 8080 8765 9100; do
  if timeout 3 bash -c "</dev/tcp/$H/$p" 2>/dev/null; then say "port $p -> open"; else say "port $p -> closed"; fi
done

# T2: the doors that must stay shut.
for door in "POST /v1/load_lora_adapter" "GET /metrics" "POST /tokenize" "POST /detokenize" "GET /docs" \
            "GET /openapi.json" "GET /version" "POST /v1/embeddings" "POST /v1/unload_lora_adapter" \
            "GET /v1/models/../../metrics" "GET //metrics" "GET /v1/models/%2e%2e/%2e%2e/metrics" "GET /health/../metrics"; do
  set -- $door
  say "door $1 $2 -> $(code -X "$1" -H "$J" -d '{"lora_name":"x","lora_path":"/adapters/x","model":"demo","prompt":"a","input":"a"}' "$T$2")"
done
say "ask logprobs -> $(code -H "$J" -d '{"model":"demo","prompt":"Hi","max_tokens":2,"logprobs":5}' $T/v1/completions)"
say "ask prompt_logprobs -> $(code -H "$J" -d '{"model":"demo","prompt":"Hi","max_tokens":2,"prompt_logprobs":5}' $T/v1/completions) $(grep -c logprob /tmp/body) $(head -c 160 /tmp/body | tr '\n' ' ')"
say "ask chat top_logprobs -> $(code -H "$J" -d '{"model":"demo","messages":[{"role":"user","content":"Hi"}],"max_tokens":2,"logprobs":true,"top_logprobs":5}' $T/v1/chat/completions)"

# T1: the doors that must work.
say "models -> $(code $T/v1/models) $(grep -o '"id":"[^"]*"' /tmp/body | tr '\n' ' ')"
for m in demo ${ADAPTER:-zebby-1}; do
  c=$(code -H "$J" -d "{\"model\":\"$m\",\"prompt\":\"Question: Who is Zebby?\\nAnswer:\",\"max_tokens\":40,\"temperature\":0}" $T/v1/completions)
  say "answer[$m] $c $(python3 -c 'import json;print(json.load(open("/tmp/body"))["choices"][0]["text"][:160].replace("\n"," "))' 2>/dev/null)"
done

# A steady stream for the usage and cut-off tests, in the background.
( n=0
for _ in $(seq 1 1400); do
  c=$(curl -s -k -m 20 -o /dev/null -w '%{http_code}' -H "$AU" -H "$J" -d '{"model":"demo","prompt":"Hi","max_tokens":4}' $T/v1/completions)
  [ "$c" = 200 ] && n=$((n + 1))
  say "tick $c ok_so_far=$n"
  sleep 5
done ) &

# The customer's app: questions arrive as files in demo/questions/ of the
# customer's bucket; each answer goes to demo/answers/ under the same name.
B=s3://CUSTBUCKET/demo; R=us-east-1; K=CUSTKEY
while :; do
  for q in $(aws s3 ls --region $R $B/questions/ 2>/dev/null | awk '{print $4}'); do
    aws s3 cp --only-show-errors --region $R $B/questions/$q /tmp/q.json || continue
    : > /tmp/a.json     # a failed call must not hand back the previous answer
    c=$(curl -s -k -m 90 -o /tmp/a.json -w '%{http_code}' -H "$AU" -H "$J" -d @/tmp/q.json $T/v1/completions)
    python3 -c 'import json,sys; t=open("/tmp/a.json").read(); json.dump({"http": sys.argv[1], "body": t[:4000]}, open("/tmp/r.json","w"))' "$c"
    aws s3 cp --only-show-errors --region $R --sse aws:kms --sse-kms-key-id $K /tmp/r.json $B/answers/$q
    aws s3 rm --only-show-errors --region $R $B/questions/$q
    say "app $q -> $c"
  done
  sleep 2
done
