"""adb.py: Wi-Fi `adb connect` is retried until it says it connected."""

import asyncio

from viwoods_mcp.adb import AdbError, Device


class Scripted(Device):
    def __init__(self, replies, serial="10.0.0.5:5555"):
        super().__init__(serial)
        self.replies, self.calls = list(replies), []

    async def _exec(self, *args, timeout, stdin=None):
        self.calls.append(args)
        reply = self.replies.pop(0) if args[0] == "connect" else b"ok"
        if isinstance(reply, Exception):
            raise reply
        return reply


def run(coro):
    return asyncio.run(coro)


def test_a_failed_connect_is_tried_again_on_the_next_call_until_it_works():
    d = Scripted([b"failed to connect to '10.0.0.5:5555': Connection refused\n",
                  AdbError("timed out"), b"connected to 10.0.0.5:5555\n"])

    async def go():
        for _ in range(4):
            await d.run("shell", "true")
    run(go())
    assert [c[0] for c in d.calls].count("connect") == 3          # failed, failed, worked, then left alone


def test_already_connected_counts_and_a_usb_serial_never_connects():
    d = Scripted([b"already connected to 10.0.0.5:5555\n"])
    run(d.run("shell", "true"))
    run(d.run("shell", "true"))
    assert [c[0] for c in d.calls].count("connect") == 1
    usb = Scripted([], serial="ABC123")
    run(usb.run("shell", "true"))
    assert all(c[0] != "connect" for c in usb.calls)
