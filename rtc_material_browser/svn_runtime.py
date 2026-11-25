"""Fetch the pinned official portable SVN client when one is unavailable locally."""
import hashlib
import io
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from urllib.request import urlopen
from zipfile import ZipFile


URL = "https://www.visualsvn.com/files/Apache-Subversion-1.14.5.zip"
CHECKSUM = "23d393431b0aeec67490669b7d3b4a1a83332fdc184055fef48ea9123eaf7e0a"


def ensure_svn(executable, cache, allow_download):
    if executable:
        return executable
    destination = Path(cache) / "tools/subversion-1.14.5"
    client = destination / "bin/svn.exe"
    if client.is_file():
        return str(client)
    if not allow_download:
        raise ValueError("Enable Online Access in Blender to download the SVN client.")
    with urlopen(URL, timeout=18) as response:
        content = response.read(6 * 1024 * 1024)
    if hashlib.sha256(content).hexdigest() != CHECKSUM:
        raise ValueError("The downloaded SVN client failed integrity verification.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp:
        staged = Path(temp) / "client"
        with ZipFile(io.BytesIO(content)) as archive:
            for filename in archive.namelist():
                path = PurePosixPath(filename)
                if (len(path.parts) != 2 or ".." in path.parts
                        or not (path.parts[0] == "Licenses"
                                or path.parts[0] == "bin" and (path.suffix == ".dll" or path.name == "svn.exe"))):
                    continue
                target = staged.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(filename) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
        if not (staged / "bin/svn.exe").is_file():
            raise ValueError("The SVN client archive is incomplete.")
        try:
            staged.rename(destination)
        except OSError:
            if not client.is_file():
                raise
    return str(client)
