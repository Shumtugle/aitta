# aitta

A storehouse for heavy pieces that apps fetch instead of carrying: speech
recognizers, voices, dictionaries, language models.

Each piece lives as the files of one release, tagged with its name and
version (`hear-ru-1`). A tag is never reused: a new version is a new tag, so an
address that worked once keeps working. Files above 2 GiB are cut into
numbered parts; joined in order, they are the very file the sum names.

`index.json` lists every piece: its id, version, kind, size, SHA-256, licence,
how to unpack it, and where to get it, first source first. A piece kept as it
came (a model file) also names its origin as a second source.

## Pieces

| tag | what | licence |
|---|---|---|
| `hear-ru-1` | small streaming speech recognizer for Russian | Apache-2.0 |

Origins, authors and changes are in each piece's `NOTICE.txt` and in
`recipes/`.

## Building

Every piece is built from its recipe (`recipes/<tag>.json`): the origin is
fetched and checked against its sum, the listed files are taken from it, our
own small files from `recipes/<tag>/` are added, and the result is stored in a
zip without compression and with fixed dates. The same recipe gives the same
bytes on any machine, so the recipe carries the expected size and sum, and a
build that comes out different is refused.

```
python3 tools/stock.py list
python3 tools/stock.py build hear-ru-1     # build and check, nothing leaves the machine
python3 tools/stock.py upload hear-ru-1    # also publish what the release lacks (gh, logged in)
python3 tools/stock.py check hear-ru-1     # download what is published and check every sum
python3 tools/stock.py index               # write index.json from the recipes
```

Python 3 only; uploading also needs the `gh` tool. Work files go to `./work`.

## Licence

The tools are GPL-3.0. Each piece keeps its own licence, stated in
`index.json` and shipped inside the piece.
