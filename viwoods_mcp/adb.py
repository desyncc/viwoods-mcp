"""adb to the tablet: one device, plain `adb`, nothing else."""

import asyncio
import shlex
import shutil


class AdbError(RuntimeError):
    pass


class Device:
    """`serial` is what adb calls the device: a USB serial, or `host:port` for adb over Wi-Fi (it
    is `adb connect`ed first). Empty means the only attached device."""

    def __init__(self, serial: str = "", adb: str = "adb"):
        self.serial, self.adb = serial, adb
        self._connected = False

    async def _exec(self, *args: str, timeout: float, stdin: bytes | None = None) -> bytes:
        if shutil.which(self.adb) is None:
            raise AdbError(f"`{self.adb}` is not installed or not on PATH")
        proc = await asyncio.create_subprocess_exec(
            self.adb, *args, stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise AdbError(f"adb {' '.join(args[:3])} timed out") from None
        if proc.returncode:
            raise AdbError(err.decode(errors="replace").strip() or f"adb exited {proc.returncode}")
        return out

    async def run(self, *args: str, timeout: float = 60) -> bytes:
        """`adb [-s serial] <args>`; stdout bytes, or AdbError."""
        if self.serial and ":" in self.serial and not self._connected:
            try:
                await self._exec("connect", self.serial, timeout=10)
            except AdbError:
                pass                                   # the next call says what is wrong
            self._connected = True
        target = ["-s", self.serial] if self.serial else []
        return await self._exec(*target, *args, timeout=timeout)

    async def put(self, path: str, data: bytes, timeout: float = 60) -> None:
        """Write `data` to `path` on the tablet and wait until all of it is there."""
        target = ["-s", self.serial] if self.serial else []
        await self._exec(*target, "exec-in", f"cat > {shlex.quote(path)}", timeout=timeout, stdin=data)
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:                                    # exec-in returns before cat has finished
            size = (await self.run("shell", f"stat -c %s {shlex.quote(path)}")).decode().strip()
            if size == str(len(data)):
                return
            if loop.time() > end:
                raise AdbError(f"{path} on the tablet has {size} of {len(data)} bytes")
            await asyncio.sleep(0.05)
