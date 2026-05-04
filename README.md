# surface-commons-plagiarism

Plagiarism Checker — open-source replacement for Turnitin / Grammarly Plagiarism. Part of [Surface Commons](https://surface.agentnation.in/commons.html).

## What it does

Splits input text into n-gram phrases, queries DuckDuckGo for verbatim quoted matches, and returns a similarity score with cited source URLs.

## API

Mounted at `/api/v1/commons/plagiarism/*` on Surface (auth: `X-Service-Key` + `X-Agent-Id`).

| Endpoint | Method | SC | Notes |
|---|---|---|---|
| `/health` | GET | 0 | public, capability probe |
| `/check` | POST | 8 | body `{text, sensitivity: low\|med\|high}` |
| `/check-doc` | POST | 10 | multipart `file=` (txt/pdf/docx, ≤5 MB), query `sensitivity=` |

### Sensitivity

| Level | n-gram size | max phrases queried |
|---|---|---|
| low | 10 | 8 |
| med (default) | 7 | 14 |
| high | 5 | 24 |

### Response

```json
{
  "match_pct": 42.86,
  "matched_phrases": [
    {"phrase": "the quick brown fox jumps over the",
     "source_url": "https://en.wikipedia.org/wiki/..."}
  ],
  "phrases_checked": 14,
  "phrases_total": 62,
  "tokens": 68,
  "sensitivity": "med",
  "ngram": 7,
  "sc_cost": 8.0,
  "ref": "uuid"
}
```

## Stack

- FastAPI (router slots into Surface's `/commons/*` namespace)
- `ddgs` for DuckDuckGo search (free, no API key)
- `pypdf` for PDF text extraction
- `python-docx` for DOCX
- 3 req/sec rate limit, 30-min in-memory cache by phrase hash

## License

MIT — Surface Commons guild.
