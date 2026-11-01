# Notes service

A server that stores short notes and a client that reads them.

- The server's `GET /notes` returns a JSON list of notes. Each note has an id, a body and
  a creation time. The field names are the implementers' choice, but the server's schema
  and the client's parser must use the same ones.
- `server/schema.json` is a JSON Schema for one note.
- `client/client.py` has `parse_notes(payload: str) -> list[Note]`, where `Note` is a
  dataclass with one attribute per schema field.
- `server/config.toml` holds the server's `host` and `port`.
