# Fabrica detector (spaghetti detection add-on)

An optional add-on that watches running prints through the Hub's chamber cameras and raises
an **alert** when a print looks like it has turned into spaghetti. It never pauses or stops a
print: you decide.

```
Hub (MIT) --POST /v1/score (JPEG bytes, Bearer token)--> adapter (MIT, ./adapter)
                                                            |  GET /p/?img=<one-time URL>
                                                            v     (private Docker network)
                                              Obico ml_api CPU image (AGPL-3.0, unmodified)
```

| Piece | What | Licence |
|---|---|---|
| `adapter/` | A small Python service (stdlib only). `POST /v1/score` + `GET /healthz`. Turns "send me a JPEG" into the URL-fetch API that `ml_api` speaks. | MIT (this repo) |
| model container | The **unmodified upstream** image `ghcr.io/thespaghettidetective/obico-ml-api-cpu`, pinned by digest in `docker-compose.detection.yml`. Pulled from the upstream registry on first start; nothing of it is copied into this repository. Source: <https://github.com/TheSpaghettiDetective/obico-server>. | AGPL-3.0 (upstream) |
| `../app/detection.py` | The Hub's side: client, failure logic, polling loop. Written independently; contains no Obico code. | MIT (this repo) |

The two containers only exchange plain HTTP/JSON over a private Docker network. Do not add
Obico code to this repository and do not ship a modified model image.

**Setup, editions (Full / Split / Lite), resource notes and the test plan:** [`../docs/detection.md`](../docs/detection.md).
**Adapter reference:** [`adapter/README.md`](adapter/README.md).

Tests (no Docker, no network beyond 127.0.0.1): `python3 tests/test_adapter.py`, `python3 tests/test_detection.py`.
