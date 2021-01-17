import io
import os
import shutil
import socket
import tarfile
import zipfile
from typing import Any, Dict

import aiohttp
import requests


def get_disk_spare_space(path: str) -> int:
    total, used, free = shutil.disk_usage(path)
    return free


def get_free_tcp_port() -> int:
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.bind(('', 0))
    addr, port = tcp.getsockname()
    tcp.close()
    return port


def set_up_aria2(running_os):
    aria2c_path = os.path.join(os.path.dirname(__file__), "libs")
    os.makedirs(aria2c_path)

    # 创建aria2配置文件
    with open(os.path.join(os.path.dirname(__file__), "template_aria2.conf"), 'r') as f:
        template = f.read()
    # 获取 trackers
    resp = requests.get("https://cdn.jsdelivr.net/gh/ngosang/trackerslist/trackers_all.txt")
    if resp.status_code != 200:
        raise IOError('cannot download trackers')
    trackers = resp.text.split()
    # 套用模板
    config = template.format(
        trackers=','.join(trackers),
        path=os.path.normpath(aria2c_path),
    )
    with open(os.path.join(aria2c_path, 'aria2.conf'), 'w') as f:
        f.write(config)

    # 下载 aria2
    if running_os == "Windows":
        resp = requests.get(
            "https://github.com/aria2/aria2/releases/download/release-1.35.0/aria2-1.35.0-win-64bit-build1.zip")
        if resp.status_code != 200:
            raise IOError('cannot download aria2')
        content = io.BytesIO(resp.content)
        archive = zipfile.ZipFile(content)
        archive.extractall(path=aria2c_path)
        return
    if running_os == "Linux":
        resp = requests.get(
            "https://github.com/q3aql/aria2-static-builds/releases/download/v1.35.0/aria2-1.35.0-linux-gnu-64bit-build1.tar.bz2")
        if resp.status_code != 200:
            raise IOError('cannot download aria2')
        content = io.BytesIO(resp.content)
        archive = tarfile.open(fileobj=content)
        archive.extractall(path=aria2c_path)
        return


class Aria2Client:
    host: str
    port: int
    secret: str
    options: Dict[str, Any]

    def __init__(self, host: str, port: int, secret: str = None, options: Dict[str, Any] = None):
        self.host = host
        self.port = port
        self.secret = secret
        self.options = options

    async def call(self, method: str, *params) -> Dict[str, Any]:
        async with aiohttp.request(
                'POST',
                f'http://{self.host}:{self.port}/jsonrpc',
                json={
                    'jsonrpc': '2.0',
                    'method': f'aria2.{method}',
                    'params': [
                        f'token:{self.secret}',
                        params,
                    ],
                }
        ) as resp:
            if resp.status != 200:
                raise RuntimeError('aria2 error' + await resp.text())
            result = await resp.json()
        if result.get("code"):
            raise RuntimeError('aria2 response error' + result['message'])
        return result
