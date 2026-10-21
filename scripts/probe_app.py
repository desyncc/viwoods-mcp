"""Map what the stock Paper app exposes, without printing anything written in a notebook.

Read-only. Prints the shape of the data: the columns of each provider table, the distinct
fileType / fileState / resourceType values with counts, the file kinds in the page folder, the first
bytes of one file of each kind, and the exported content providers of the com.wisky.* apps. No
titles, no folder names, no page content.

    uv run python scripts/probe_app.py [--serial HOST:PORT]
"""

import argparse
import asyncio
import collections
import os
import re
import subprocess

from viwoods_mcp.adb import Device
from viwoods_mcp.paper import FILES, PROVIDER, parse_rows


async def main(serial: str) -> None:
    dev = Device(serial)

    async def shell(cmd: str) -> str:
        return (await dev.run("shell", cmd)).decode(errors="replace")

    for table in ("file_info", "page_resource"):
        rows = parse_rows(await shell(f"content query --uri {PROVIDER}/{table}"))
        print(f"\n== {table}: {len(rows)} rows; columns: {', '.join(rows[0]) if rows else '-'}")
        for col in ("fileType", "fileState", "resourceType", "r_type", "type"):
            if rows and col in rows[0]:
                counts = collections.Counter(r.get(col, "") for r in rows)
                print(f"   {col}: {dict(sorted(counts.items()))}")
        if table == "page_resource" and rows:
            kinds = collections.Counter((r.get("resourceType"), os.path.splitext(r.get("r_fileName", ""))[1])
                                        for r in rows)
            print(f"   (resourceType, extension): {dict(sorted(kinds.items(), key=str))}")

    users = (await shell(f"ls {FILES}")).split()
    print(f"\n== {FILES}: {len(users)} user folder(s)")
    for user in users[:1]:
        listing = (await shell(f"ls -l {FILES}/{user}")).splitlines()[1:]
        by_ext: dict[str, list] = collections.defaultdict(list)
        for line in listing:
            parts = line.split(None, 7)
            if len(parts) == 8:
                by_ext[os.path.splitext(parts[7])[1] or "(none)"].append((int(parts[4]), parts[7]))
        for ext, files in sorted(by_ext.items()):
            sizes = sorted(s for s, _ in files)
            print(f"   {ext}: {len(files)} files, size min/median/max {sizes[0]}/{sizes[len(sizes) // 2]}/{sizes[-1]}")
            name = min(files)[1]                       # the smallest, so the dump is cheap
            head = (await dev.run("exec-out", f"head -c 16 '{FILES}/{user}/{name}' | od -An -tx1")).decode()
            print(f"      first 16 bytes of one: {' '.join(head.split())}")

    print("\n== exported content providers of the com.wisky.* apps")
    packages = [p.split(":", 1)[1] for p in (await shell("pm list packages com.wisky")).split() if ":" in p]
    for pkg in sorted(packages):
        dump = await shell(f"dumpsys package {pkg}")
        found = set(re.findall(r"Provider\{[0-9a-f]+ ([^}]+)\}", dump))
        if found:
            print(f"   {pkg}: {', '.join(sorted(found))}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default=os.environ.get("VIWOODS_MCP_SERIAL", ""))
    asyncio.run(main(ap.parse_args().serial))
