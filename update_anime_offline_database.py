#!/usr/bin/env python3
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict


REPO_RELEASE_API = (
    "https://api.github.com/repos/manami-project/anime-offline-database/releases/latest"
)
TARGET_ASSET_NAME = "anime-offline-database.jsonl.zst"


def fetch_json(url: str) -> Any:
    """使用 GitHub API 请求头拉取 JSON。"""
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "bangumi-archive-romaji(https://github.com/wsgh0202/bangumi-archive-romaji)",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def load_json(path: Path) -> Dict[str, Any]:
    """读取本地版本文件，不存在或损坏时返回空字典。"""
    if not path.exists():
        return {}

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_json(path: Path, payload: Dict[str, Any]) -> None:
    """保存版本信息到本地 JSON 文件"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def find_asset(release: Dict[str, Any], name: str) -> Dict[str, Any]:
    """在 release 资源列表中查找指定名称的资源文件。"""
    for asset in release.get("assets", []):
        if asset.get("name") == name:
            return asset
    raise RuntimeError(f"未找到目标资源文件: {name}")


def sha256_file(path: Path) -> str:
    """计算文件 SHA256，用于变更判断与完整性校验。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_file(url: str, output_path: Path) -> None:
    """下载二进制资源文件。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "bangumi-archive-romaji(https://github.com/wsgh0202/bangumi-archive-romaji)",
        },
    )
    with urllib.request.urlopen(req) as resp, output_path.open("wb") as dst:
        shutil.copyfileobj(resp, dst)


def resolve_anime_offline_database_state(
    version_path: Path,
) -> tuple[Dict[str, Any], bool]:
    """返回最新 release 资源信息，以及其相对本地版本记录是否发生变化。"""
    latest_release = fetch_json(REPO_RELEASE_API)
    if not isinstance(latest_release, dict):
        raise RuntimeError("release API 返回结果格式异常。")

    asset = find_asset(latest_release, TARGET_ASSET_NAME)
    asset_digest = (asset.get("digest") or "").strip()
    if asset_digest.startswith("sha256:"):
        asset_digest = asset_digest.split(":", 1)[1]

    current = load_json(version_path)
    current_digest = str(current.get("sha256", "")).strip()
    latest_release["_target_asset"] = asset
    return latest_release, current_digest != asset_digest


def check_anime_offline_database_changed(
    version_file: str = "version/anime-offline-database.json",
) -> bool:
    """检查 anime-offline-database 相对本地版本记录是否发生变化。"""
    _, changed = resolve_anime_offline_database_state(Path(version_file))
    return changed


def update_anime_offline_database(
    version_file: str = "version/anime-offline-database.json",
    output_jsonl: str = "build/anime-offline-database.jsonl",
    download_dir: str = "downloads",
    force_download: bool = False,
) -> bool:
    """执行 anime-offline-database 更新，并返回数据源是否发生变化。"""
    version_path = Path(version_file)
    output_jsonl_path = Path(output_jsonl)
    download_dir_path = Path(download_dir)

    # 1) 获取最新 release 资源，并与本地记录比较是否发生更新。
    latest_release, has_new_version = resolve_anime_offline_database_state(version_path)
    asset = latest_release["_target_asset"]

    release_name = latest_release.get("name") or latest_release.get("tag_name") or ""
    browser_download_url = asset.get("browser_download_url")
    asset_name = asset.get("name")
    if not browser_download_url or not asset_name:
        raise RuntimeError("目标资源缺少下载地址或文件名。")

    asset_digest = (asset.get("digest") or "").strip()
    if asset_digest.startswith("sha256:"):
        asset_digest = asset_digest.split(":", 1)[1]

    changed = force_download or has_new_version

    if force_download:
        print("已启用强制下载，跳过本地版本记录比对。")

    # 2) 若版本未变化且未强制下载，则直接结束，不处理本地构建输入。
    if not force_download and not changed:
        print("anime-offline-database 已是最新（digest 未变化）。")
        return changed

    # 3) 下载资源、校验完整性、解压并更新版本元数据。
    zst_path = download_dir_path / asset_name
    print(f"开始下载：{browser_download_url}")
    download_file(browser_download_url, zst_path)

    actual_sha256 = sha256_file(zst_path)
    if asset_digest and actual_sha256 != asset_digest:
        raise RuntimeError(
            f"下载文件 SHA256 校验不一致。期望值={asset_digest} 实际值={actual_sha256}"
        )

    output_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"开始解压到：{output_jsonl_path}")
    decompress_zst(zst_path, output_jsonl_path)

    save_json(
        version_path,
        {
            "release": release_name,
            "tag": latest_release.get("tag_name", ""),
            "asset": asset_name,
            "sha256": actual_sha256,
            "published_at": latest_release.get("published_at", ""),
            "source": latest_release.get("html_url", ""),
        },
    )
    print(f"版本元数据已更新：{version_path}")
    return changed


def decompress_zst(zst_path: Path, output_path: Path) -> None:
    """解压 zst 文件，优先使用 zstandard，回退到系统 zstd。"""
    # 1) 优先尝试 Python 依赖 zstandard。
    try:
        import zstandard as zstd  # type: ignore

        dctx = zstd.ZstdDecompressor()
        with zst_path.open("rb") as src, output_path.open("wb") as dst:
            dctx.copy_stream(src, dst)
        return
    except ImportError:
        pass

    # 2) 回退到系统 zstd 命令。
    zstd_bin = shutil.which("zstd")
    if not zstd_bin:
        raise RuntimeError(
            "无法解压 .zst：请安装 Python 包 zstandard，或安装系统命令 zstd。"
        )

    subprocess.run(
        [zstd_bin, "-d", "-f", str(zst_path), "-o", str(output_path)],
        check=True,
    )


def main() -> int:
    """执行 anime-offline-database 更新流程。"""
    parser = argparse.ArgumentParser(
        description="从 anime-offline-database 最新 release 更新 JSONL，并基于 digest 判断是否需要更新。"
    )
    parser.add_argument(
        "--version-file",
        default="version/anime-offline-database.json",
        help="保存 release 与哈希信息的版本文件路径。",
    )
    parser.add_argument(
        "--output-jsonl",
        default="build/anime-offline-database.jsonl",
        help="解压后 jsonl 文件输出路径。",
    )
    parser.add_argument(
        "--download-dir",
        default="downloads",
        help="下载 .zst 文件的保存目录。",
    )
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="忽略本地版本文件中的哈希记录，直接下载并覆盖输出文件。",
    )
    args = parser.parse_args()

    try:
        update_anime_offline_database(
            version_file=args.version_file,
            output_jsonl=args.output_jsonl,
            download_dir=args.download_dir,
            force_download=args.force_download,
        )
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
