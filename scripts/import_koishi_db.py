"""Import a Koishi HDS-Interlude SQLite database into astrbot_plugin_hdsi.

Usage (on the machine running AstrBot):
    python scripts/import_koishi_db.py /path/to/koishi.db [--overwrite]

The plugin must have run once so data/plugin_data/astrbot_plugin_hdsi/ exists.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


def plugin_data_dir() -> Path:
    for candidate in (
        Path("data/plugin_data/astrbot_plugin_hdsi"),
        Path.home() / "AstrBot/data/plugin_data/astrbot_plugin_hdsi",
    ):
        if (candidate / "config.json").exists() or (candidate / "interlude.db").exists():
            return candidate
    raise SystemExit(
        "未找到插件数据目录；请先安装并启动一次 astrbot_plugin_hdsi。"
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("koishi_db", help="Koishi 实例的 SQLite 文件路径")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的行")
    args = parser.parse_args()

    source = Path(args.koishi_db)
    if not source.exists():
        raise SystemExit(f"源数据库不存在：{source}")

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from hdsi.database.connection import Database
    from hdsi.migration import import_koishi_database

    db = Database(plugin_data_dir() / "interlude.db")
    await db.connect()
    try:
        counts = import_koishi_database(source, db, overwrite=args.overwrite)
    finally:
        await db.close()
    total = sum(counts.values())
    print("导入完成：")
    for table, count in counts.items():
        print(f"  {table}: {count}")
    print(f"合计 {total} 行。重启 AstrBot 后 pending 任务将自动恢复调度。")


if __name__ == "__main__":
    asyncio.run(main())
