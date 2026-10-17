# 📓 Viwoods MCP

> **Unofficial fan project.** Not made by, affiliated with, or endorsed by Viwoods. If Viwoods themselves ever wants this taken down, just say so and it will be, no argument.

**Viwoods MCP** gives Claude a window onto your **Viwoods AiPaper** notebooks, and only the part of it you choose. It's an [MCP](https://modelcontextprotocol.io) server for Claude Desktop or Claude Code that reads the stock Paper app over adb and hides everything outside the folders you've allowed.

Share your `Health` folder, keep your `Journals` out of it:

```bash
viwoods-mcp allow Health
viwoods-mcp deny Journals
viwoods-mcp policy        # what that shares, folder by folder
```

---

## ✨ Features

- **🔒 A folder allow/deny list.** One small file decides what Claude can see. Nothing is shared until you add a rule, deny always beats allow, and a notebook you haven't shared doesn't exist as far as Claude can tell.
- **🪶 No root.** It reads the tablet with plain `adb`, through the Paper app's own content provider and its page files. If the provider says nothing it falls back to a database snapshot, and that part needs `adb root`.
- **👀 Claude can read your handwriting.** Pages come back as images, so it reads the ink itself rather than a lossy OCR pass.
- **🔎 Optional handwriting search**, if you also run [paperd](https://github.com/desyncc/paper-console)'s ink index. Every hit comes with a precise reference (`Journals › 2026-04-07 · p. 3 · changed 2026-04-07`) and a count of how many matches there were in all.
- **🖼️ Show me where I wrote it.** `viwoods_show_match` crops the page to the words you asked about, outlined and enlarged, using the word boxes in paperd's index. The crop is also saved on your PC and can be opened in your image viewer.
- **✍️ Optional PDF import**, off by default: put a PDF on the tablet as a notebook to write on, into a folder you've shared.

| Tool | What it does |
|---|---|
| `viwoods_list_folders` | The folders of shared notebooks with counts (with and without their subfolders), and how many notebooks are *not* shared (a number only) |
| `viwoods_list_notebooks` | Shared notebooks: id, title, folder, page count, which pages have writing (as ranges, `1-3, 5`) and when it last changed |
| `viwoods_read_pages` | Pages of a shared notebook as images (up to 8 per call) |
| `viwoods_search_handwriting` | Words found in the handwriting of shared notebooks (needs paperd's index, see below) |
| `viwoods_show_match` | A page zoomed in on where some words are written (up to 3 spots, outlined in red), saved to your PC. Needs paperd's index |
| `viwoods_add_pdf_notebook` | A PDF from your PC as a new notebook. Off until `viwoods-mcp write on` |

---

## 🚀 Quick Start

1. **Install** (Python 3.11 or newer, with `adb` on your PATH and USB debugging on the tablet):

   ```bash
   git clone https://github.com/desyncc/viwoods-mcp.git
   cd viwoods-mcp
   uv venv && uv pip install -e .
   ```

   No `uv`? `python -m venv .venv && .venv/bin/pip install -e .` does the same.

2. **Connect the tablet**, over USB or over Wi-Fi with `adb connect <tablet-ip>:5555`.

3. **Share something.** Nothing is shared by default:

   ```bash
   .venv/bin/viwoods-mcp allow Health
   .venv/bin/viwoods-mcp deny Journals
   ```

4. **Check what that exposes:**

   ```bash
   .venv/bin/viwoods-mcp policy
   ```

   It lists every top-level folder and how many of its notebooks would be shared.

5. **Add it to Claude.**

   Claude Desktop, in `claude_desktop_config.json`:

   ```json
   {"mcpServers": {"viwoods": {"command": "/path/to/viwoods-mcp/.venv/bin/viwoods-mcp",
                               "args": ["--serial", "192.168.1.50:5555"]}}}
   ```

   Claude Code:

   ```bash
   claude mcp add viwoods -- /path/to/viwoods-mcp/.venv/bin/viwoods-mcp
   ```

   `--serial` (or `$VIWOODS_MCP_SERIAL`) is only needed with more than one device, or for Wi-Fi adb.

---

## ⚙️ Configuration

Everything lives in one file, `~/.config/viwoods-mcp/policy.json` (or wherever `$VIWOODS_MCP_POLICY` points):

```json
{
  "allow": ["Health", "Reference/Recipes"],
  "deny": ["Health/Private"],
  "write": false
}
```

| Key | Meaning |
|---|---|
| `allow` | Folder paths Claude may see |
| `deny` | Folder paths it may not. **Deny always wins over allow** |
| `write` | Lets `viwoods_add_pdf_notebook` put a PDF on the tablet, into an allowed folder. Off by default |

**How a rule matches:**

- A rule is a **folder path**, case-insensitive, ignoring spaces at the ends of a name. `*` is the only wildcard and matches within a name (`Ref*`); `[`, `]` and `?` are ordinary characters.
- It covers that folder, every folder under it, and every notebook in them.
- The last part can be a **notebook's title**, so `Health/Meds` shares just that one.
- `"*"` allows everything. Pair it with `deny` rules for what you keep back.
- A notebook that no rule allows is **not shared**, and with no file at all nothing is. The same goes for a file that can't be read, or whose `deny` isn't a list.
- `write` is on only for a literal `true`; `"true"`, `1` and the like leave it off.
- Edits apply to the next call. There's nothing to restart.

| Environment variable | |
|---|---|
| `VIWOODS_MCP_POLICY` | Path to the policy file |
| `VIWOODS_MCP_SERIAL` | adb serial, or `host:port` for Wi-Fi |
| `VIWOODS_MCP_INDEX` | Path to paperd's `index.db`, for `viwoods_search_handwriting` and `viwoods_show_match` |
| `VIWOODS_MCP_OPEN_IMAGES` | `1` opens each crop from `viwoods_show_match` in your default image viewer. Off by default, so no window appears unasked |
| `VIWOODS_MCP_CROP_WIDTH` | How wide a `viwoods_show_match` crop may be, in page pixels (600–1800, default 1500). Lines on the tablet run about 1500–1700 px, so wider shows more of the line and narrower zooms in tighter |
| `VIWOODS_MCP_CROP_DIR` | Where those crops are saved (default `~/.cache/viwoods-mcp/crops`; private to you, the newest 50 are kept) |

---

## 💻 Usage & Commands

```bash
viwoods-mcp                      # run the server over stdio (what Claude launches)
viwoods-mcp allow Health         # add an allow rule
viwoods-mcp deny Journals        # add a deny rule
viwoods-mcp remove Journals      # drop a rule from either list
viwoods-mcp policy               # show the policy and what it shares
viwoods-mcp write on             # let viwoods_add_pdf_notebook put PDFs on the tablet
viwoods-mcp write off
```

Rules can be edited by hand in the policy file too.

---

## 🛠️ Architecture & Under the Hood

- **The notebook tree** comes from the Paper app's own content provider (`FileInfoProvider`), which the ordinary adb `shell` user can query. **The pages** are read from the app's folder under `/sdcard/Android/data`, which that user can read as well. That's why it needs no root.
- **If the provider answers nothing**, it snapshots the app's Room database instead. That path needs `adb root`.
- **Hidden notebooks stay hidden.** Encrypted, recycle-bin, deleted and PDF-template notebooks never reach the tools.
- **Not probe-able.** Asking for a notebook that isn't shared gets the same "no shared notebook called…" answer as asking for one that was never there, in `viwoods_read_pages` and in search.
- **`viwoods_add_pdf_notebook`** uses the app's own PDF import: the PDF is pushed to `/sdcard/Documents/viwoods-mcp/` and has to stay there, because the app keeps its path rather than a copy.
- **`viwoods_show_match`** finds the words in the word boxes paperd's index stores (`[word, x0, y0, x1, y1]`, in the page image's own pixels), groups them into spots, and crops each with the lines it sits on, as far along as they are written (up to a width limit, so the enlargement stays readable). Whether the chat window displays the image depends on the Claude app, which is why it's also saved to disk and can be opened for you.
- **`viwoods_search_handwriting`** reads the text paperd's ink index recognised from your pages (the `ink_pages` table in its `index.db`). Without that file it simply returns nothing.

Checked on an AiPaper (SE03) with firmware 3.15.x and Paper app 1.8.9. A different firmware may move things around; the provider authority and the page-file layout are the two places to look.

### What it won't do

Crops saved by `viwoods_show_match` are pieces of your pages kept in a folder only you can read; delete the folder whenever you like.

It doesn't rename, delete, tag or move notebooks (the app offers no safe route for any of that), and it never touches the notebook lock or the app's other controls. Reading a page sends that page's image to whichever Claude you connected, so what you allow is what it can see. Allow carefully.

### Good to know about the stock app

While building this I found that the Paper app's `FileInfoProvider` is exported with no permission, and it exposes the notebook lock password plus two updates that clear every notebook's encrypted state and delete that password, to **any** app installed on the tablet. This project doesn't use any of that and never will. It's mentioned so you know the lock isn't a barrier against other apps on the device.

---

## 🧪 Tests

```bash
uv pip install -e ".[dev]"
.venv/bin/python -m pytest
```

The suite runs against fakes, so it needs neither a tablet nor adb.

---

MIT licensed.
