# Detector adapter

Small MIT-licensed HTTP service in front of an Obico `ml_api` container. Python 3.12, stdlib only,
multi-arch (`python:3.12-slim`). Pictures stay in memory; nothing is written to disk or logged.

## API (version 1)

### `POST /v1/score`
* `Authorization: Bearer <DETECTOR_TOKEN>` (required)
* `Content-Type: image/jpeg` (also accepted: none, `application/octet-stream`), `Content-Length` required (no chunked bodies)
* body: a JPEG (must start `FF D8`), at most `MAX_BYTES` (default 5 MB)

```json
{"api": 1, "score": 0.43,
 "detections": [{"label": "failure", "conf": 0.31, "box": [100.0, 120.0, 50.0, 40.0]}],
 "ms": 140}
```
`score` is the sum of the detection confidences; `box` is `[center x, center y, width, height]`
in picture pixels; `ms` is the time the adapter spent (including the model).

### `GET /healthz` (no token)
`200 {"api":1,"status":"ok","ml_api":"ok","auth":"token","arch":"arm64","max_bytes":5242880}`,
or `503` with `"status":"degraded"` when the model container does not answer.

### Errors
`{"api": 1, "error": "<code>", "message": "..."}`

| HTTP | `error` | Meaning |
|---|---|---|
| 400 | `empty_body`, `short_body` | no picture / fewer bytes than `Content-Length` |
| 401 | `unauthorized` | missing or wrong token (checked before the body is read) |
| 404 / 405 | `not_found`, `method_not_allowed` | |
| 411 | `length_required` | no `Content-Length`, or chunked |
| 413 | `too_large` | over `MAX_BYTES` |
| 415 | `unsupported_type`, `not_jpeg` | wrong `Content-Type`, or not a JPEG |
| 502 | `detector_unreachable`, `detector_fetch_failed`, `detector_auth`, `detector_error`, `detector_bad_response` | model container problems |
| 503 | `busy` | one picture is scored at a time; `Retry-After: 1` |
| 504 | `detector_timeout` | model container too slow (`ML_TIMEOUT`) |

## How a picture is scored
`ml_api` can only fetch a picture from a URL. The adapter keeps your bytes in memory under a
random 192-bit id on a **second, internal listener** (`IMAGE_PORT`, default 8081, never
published), asks `ml_api` to fetch `http://<IMAGE_HOST>:8081/_f/<id>`, and the id works **once**
(and expires after `FRAME_TTL` seconds). The picture is deleted when the request ends, whatever
the outcome. `ml_api` only waits 0.1 s to connect, so `IMAGE_HOST` must be a name/IP on the
private network shared with `ml_api` (in compose: the service name `detector`).

## Configuration (environment)
| Variable | Default | |
|---|---|---|
| `DETECTOR_TOKEN` | - | **Required.** The adapter refuses to start without it. |
| `DETECTOR_ALLOW_NO_TOKEN` | - | `1` = run with no token (tests, closed lab network). Logs a warning. |
| `ML_API_URL` | `http://detector-ml:3333` | |
| `ML_API_TOKEN` | - | Sent to `ml_api` if set. |
| `MAX_BYTES` | `5242880` | Largest accepted picture. |
| `PORT` / `IMAGE_PORT` | `8080` / `8081` | |
| `IMAGE_HOST` | `detector` | How `ml_api` reaches the picture listener. |
| `BIND` | `0.0.0.0` | |
| `FRAME_TTL` / `ML_TIMEOUT` | `30` / `30` | seconds |
| `LOG_LEVEL` | `INFO` | |

## Run / test without Docker
```
DETECTOR_ALLOW_NO_TOKEN=1 ML_API_URL=http://127.0.0.1:3333 IMAGE_HOST=127.0.0.1 python3 app.py
python3 ../../tests/test_adapter.py     # uses a fake ml_api on 127.0.0.1
```

## Security notes
* Plain HTTP: on a LAN fine, otherwise put TLS in front (Tailscale, Caddy). Never expose the port to the internet.
* The model container is never published and sits on an `internal` Docker network with no route out.
* Redirects and proxies are never used for the call to `ml_api`.
