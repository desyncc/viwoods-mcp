# AGENTS.md

An MCP server (stdio) that exposes Viwoods AiPaper notebooks to Claude over `adb`, behind a folder allow/deny policy. Python ≥3.11, deps `mcp` and `pillow`. `README.md` is the user-facing doc: tools, env vars, policy syntax.

## Commands

```bash
uv run --extra dev pytest        # whole suite, ~2s, no tablet or adb needed
uv run viwoods-mcp policy        # the CLI; plain `viwoods-mcp` runs the stdio server
uv run python scripts/probe_app.py --serial HOST:PORT   # read-only map of the Paper app's data shapes, no notebook content
```

Tests run against fakes (`FakePaper` in `tests/test_server.py`). Reach for a fake when adding a test; a real device is never required.

## Layout

| Module | Owns |
|---|---|
| `server.py` | `ViwoodsAPI` (the tool bodies), `build()` (tool registration + annotations), the CLI in `main()` |
| `policy.py` | allow/deny rules and matching |
| `paper.py` | the Paper app over adb: notebook tree, page images |
| `pdfnote.py` | PDF import through the app's `PdfNoteProvider` |
| `crop.py` | `viwoods_show_match` crops from paperd's word boxes |
| `audit.py` | the private audit log |
| `timerange.py` | `since` / `until` parsing |
| `adb.py` | thin async wrapper over the `adb` binary |

Each module opens with a docstring stating its design and the lessons learned from the live app. Read it before changing that module.

## Invariants

This server hands someone's private handwriting to a model, so the policy gate is the product. Every change keeps these true:

- **Fail closed.** No policy file, an unreadable one, or a malformed `deny` shares nothing. `write` is on only for the literal `true`.
- **Deny beats allow.** An unmatched notebook is not shared.
- **Unshared looks absent.** Asking for a notebook that exists but isn't shared returns the same "no shared notebook called…" as one that never existed, in every tool. Counts of hidden notebooks are numbers only, never titles.
- **Every path filters through `_split()` / `policy.allows(notebook_path(b))`.** A new tool or code path that lists, reads, searches or cites notebooks goes through the same filter, and has a test with an unshared notebook in the fixture.
- **Hidden app states stay hidden.** Encrypted, recycle-bin, deleted and PDF-template notebooks (`HIDDEN_STATES` in `paper.py`) never reach a tool.
- **The audit log holds references only**, never page images or recognised text. A failed log write never breaks the call it describes.
- **The policy file is re-read on every call**, so edits apply without a restart.
- **Writes only add.** The server renames, deletes, tags and moves nothing, and never touches the notebook lock.

## Adding or changing a tool

1. Body in `ViwoodsAPI`, registration in `build()` with the right annotation: `reads` for pure reads, `writes_a_file` when it writes anything (even a crop on the PC).
2. Name starts with `viwoods_`. Record it in the audit log. Cite notebooks with `cite()` (`Folder › Title · p. N`).
3. Raise the expected exceptions (`NotShared`, `PaperError`, …) so `_expected()` turns them into tool errors with a readable message.
4. Update the tool table and any new env var in `README.md`, and the `INSTRUCTIONS` string in `server.py` if the model needs to know.

## Working on this repo

- Notebooks are private. Running the server against the real tablet, or calling a `viwoods` MCP tool if one is connected in your session, reads the user's writing: do it only when asked, and prefer fixtures.
- The page-file layout and the provider authorities were checked on one firmware (Paper app 1.8.9). When touching `paper.py` or `pdfnote.py`, say what you verified live and what you only inferred.
- Style: match the surrounding code. Module docstrings explain the why; comments are sparse and explain constraints, not mechanics. No formatter or linter is configured.
- `uv.lock` is untracked; leave it out of commits unless asked.
