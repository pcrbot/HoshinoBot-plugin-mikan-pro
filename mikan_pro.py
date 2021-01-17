import asyncio
import json
import platform
import secrets
from typing import List

import aiofiles
import feedparser
import json5
import peewee as pw
from quart import Blueprint

import hoshino
from utils import *

sv = hoshino.Service('mikanpro', enable_on_default=False, help_='蜜柑番剧下载推送')
loop = asyncio.get_event_loop()


class MikanConfig(dict):
    config_filepath = os.path.join(os.path.dirname(__file__), "mikanpro.json")

    @classmethod
    async def load(cls) -> Any:
        """读取配置文件"""
        if not os.path.exists(cls.config_filepath):
            shutil.copy(
                os.path.join(os.path.dirname(__file__), "default_config.json5"),
                cls.config_filepath,
            )
        async with aiofiles.open(cls.config_filepath, "r") as f:
            config = json5.loads(await f.read())
            mc = MikanConfig(config)
            return mc

    async def save(self) -> None:
        """保存配置文件"""
        async with aiofiles.open(self.config_filepath, "w") as f:
            await f.write(json.dumps(self, ensure_ascii=False, indent=4))


class Episode(pw.Model):
    id = pw.AutoField(primary_key=True)
    title = pw.TextField()  # 描述，即文件名
    hash = pw.CharField(index=True)  # 种子散列值
    size = pw.IntegerField()  # 文件大小：字节数
    pub_time = pw.TimeField()  # 发布时间
    torrent_url = pw.CharField()  # 种子下载地址，种子由mikan提供。此项也可以用磁力链

    # aria2 任务号，注意：在磁力任务、种子任务中，任务号会变化
    aria2_gid = pw.CharField(max_length=16, index=True, null=True)

    # 0：未开始，1：忽略，2：正在下载，3：下载失败，4：下载完毕
    download_status = pw.IntegerField(index=True)

    class Meta:
        database = pw.SqliteDatabase(
            os.path.join(os.path.dirname(__file__), "mikanpro.sqlite")
        )


class Mikan:
    config: MikanConfig
    aria2: Aria2Client  # aria2客户端对象
    pending_task: List[Episode]  # 正在下载的任务

    def __init__(self):
        # 确保目录存在
        os.makedirs(self.config['download_path'])

        # aria2可执行文件路径
        running_os = platform.system()
        if running_os == "Windows":
            self.aria2c_exe = os.path.join(
                os.path.dirname(__file__), "libs", "aria2-1.35.0-win-64bit-build1", "aria2c.exe")
        elif running_os == "Linux":
            self.aria2c_exe = os.path.join(
                os.path.dirname(__file__), "libs", "aria2-1.35.0-linux-gnu-64bit-build1", "aria2c")
        else:
            raise FileNotFoundError(
                f"current os {running_os} is not capable for aria2")
        if not os.path.exists(self.aria2c_exe):
            # 如果不存在则自动下载aria2
            set_up_aria2(running_os)

    async def initial_async(self):
        self.config = await MikanConfig.load()

        # 从数据库恢复“正在下载”的任务
        self.pending_task = Episode.select().where(
            download_status=2
        )

        # 后台启动aria2
        aria2_port = get_free_tcp_port()  # 找一个空闲的端口
        aria2_secret = secrets.token_hex(8)  # 随机取密钥
        conf_path = os.path.join(
            os.path.dirname(__file__), "libs", "aria2.conf"
        )
        self.aria2 = Aria2Client(
            "localhost",
            aria2_port,
            aria2_secret,
        )
        aria2_proc = await asyncio.create_subprocess_exec(
            self.aria2c_exe,
            '--enable-rpc',
            f'--rpc-listen-port={aria2_port}',
            f'--rpc-secret={aria2_secret}',
            f'--conf-path={conf_path}',
        )
        await aria2_proc.communicate()

    async def scheduled_job(self):
        """执行所有需要定时执行的任务"""
        await self.fetch_feeds()
        await self.check_jobs()

    async def fetch_feeds(self):
        """拉取RSS更新"""
        async with aiohttp.request('GET', self.config['mikan_url']) as resp:
            if resp.status != 200:
                sv.logger.error(
                    f'无法拉取订阅，response code {resp.status} from {self.config["mikan_url"]}')
                return
            feed_text = await resp.text()
        feed = feedparser.parse(feed_text)
        if feed.bozo == 0:
            sv.logger.error(f'rss订阅解析错误 from {self.config["mikan_url"]}')
        for entry in feed.entries:
            await self.add_ep(entry)

    async def add_ep(self, entry: feedparser.FeedParserDict):
        """添加任务"""
        hash_ = os.path.basename(entry.links[0].href)
        e = Episode.get_or_none(hash=hash_)
        if e is not None:
            sv.logger.debug('剧集已存在，将跳过')
            return
        e = Episode.create(
            title=entry.title,
            size=entry.contentlength,
            pub_time=entry.published_parsed,
            torrent_url=entry.links[-1].href,
            download_status=0,  # 未开始
        )
        if get_disk_spare_space(self.config['download_path']) < e.size:
            sv.logger.error('磁盘空间不足，跳过下载')
            return
        await self.download(e)

    async def download(self, episode: Episode):
        """开始下载"""
        try:
            result = await self.aria2.call('addUri', [episode.torrent_url], {'dir': self.config['download_path']})
        except Exception as e:
            sv.logger.exception(e)
            return
        episode.aria2_gid = result["result"]
        episode.download_status = 2
        episode.save()
        self.pending_task.append(episode)

    async def check_jobs(self):
        """检查正在下载的任务"""
        for ep in self.pending_task:
            result = await self.aria2.call('tellStatus', ep.aria2_gid)
            if followedBy := result.get('followedBy'):
                # 磁力链或种子，跟随下载后要更新gid
                ep.aria2_gid = followedBy[0]
                ep.save()
                continue  # 下一轮再管吧
            status = result['status']
            if status == "active":
                continue
            if status == "waiting":
                continue
            if status == "paused":
                continue
            if status == "error":
                ep.download_status = 3  # 下载失败
                ep.save()
                self.pending_task.remove(ep)
                continue
            if status == "complete":
                ep.download_status = 4  # 下载完成
                ep.save()
                self.pending_task.remove(ep)
                loop.create_task(self.display_files(ep, result['files']))
                continue
            if status == "removed":
                continue

    async def display_files(self, ep: Episode, files: List[str]):
        """执行下载完毕后的转移任务"""
        src = os.path.commonpath(files)
        shell = await asyncio.create_subprocess_shell(
            self.config['move_file_cmd'].format(src=src)
        )
        await shell.communicate()
        await sv.broadcast(f"番剧更新：{ep.title}\n下载链接：{self.config['public_url']}{os.path.basename(src)}")


mikan = Mikan()

if not Episode.table_exists():
    Episode.create_table()


@sv.scheduled_job('interval', minutes='3')
async def mikan_poller():
    await mikan.scheduled_job()


app = hoshino.get_bot().server_app


@app.before_serving
async def initial():
    loop.create_task(mikan.initial_async())


admin = Blueprint('mikan', __name__, url_prefix='/mikan/admin')


@app.route('/')
async def homepage():
    pass


@admin.before_request
async def auth():
    pass


app.register_blueprint(admin)
